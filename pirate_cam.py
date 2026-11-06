#!/usr/bin/env python3
"""Pirate-Cam: use a phone as a Linux webcam and microphone.

The phone opens a web page, streams JPEG frames (and optional PCM audio) over
a WebSocket, and this server pipes them into ffmpeg, which writes to a
v4l2loopback device. Audio goes to a virtual PulseAudio/PipeWire microphone.
Python standard library only; ffmpeg and v4l2-ctl are needed at runtime.
"""
import argparse
import base64
import collections
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 16 * 1024 * 1024
KIND_VIDEO, KIND_AUDIO = 1, 2

DEFAULT_SETTINGS = {
    "mirror": False,
    "flip": False,
    "rotate": 0,
    "fit": "pad",
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gray": False,
}


def log(msg):
    print(f"[pirate-cam] {msg}", flush=True)


# --------------------------------------------------------------------------
# Picture settings -> ffmpeg filter chain
# --------------------------------------------------------------------------

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clean_settings(raw, current):
    """Merge untrusted client settings into `current`, clamping every value."""
    out = dict(current)
    for key, lo, hi in (("brightness", -1.0, 1.0), ("contrast", 0.0, 3.0), ("saturation", 0.0, 3.0)):
        if _is_number(raw.get(key)):
            out[key] = max(lo, min(hi, float(raw[key])))
    for key in ("mirror", "flip", "gray"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    rotate = raw.get("rotate")
    if isinstance(rotate, int) and not isinstance(rotate, bool) and rotate in (0, 90, 180, 270):
        out["rotate"] = rotate
    if raw.get("fit") in ("pad", "fill"):
        out["fit"] = raw["fit"]
    return out


def build_filter(settings, width, height):
    chain = []
    rotate = settings["rotate"]
    if rotate == 90:
        chain.append("transpose=1")
    elif rotate == 180:
        chain.append("hflip,vflip")
    elif rotate == 270:
        chain.append("transpose=2")
    if settings["mirror"]:
        chain.append("hflip")
    if settings["flip"]:
        chain.append("vflip")
    if settings["gray"]:
        chain.append("hue=s=0")
    b, c, s = settings["brightness"], settings["contrast"], settings["saturation"]
    if (b, c, s) != (0.0, 1.0, 1.0):
        chain.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}")
    if settings["fit"] == "fill":
        chain.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
        chain.append(f"crop={width}:{height}")
    else:
        chain.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
        chain.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    chain.append("format=yuv420p")
    return ",".join(chain)


# --------------------------------------------------------------------------
# Video sink: latest-frame-wins queue feeding a long-lived ffmpeg process
# --------------------------------------------------------------------------

class Sink:
    def __init__(self, device, width, height, verbose=False):
        self.device = device
        self.width = width
        self.height = height
        self.verbose = verbose
        self.settings = dict(DEFAULT_SETTINGS)
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0
        self.bad = 0
        self.proc = None
        self._frames = collections.deque()
        self._restart = False
        self._closed = False
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="sink", daemon=True)
        self._thread.start()

    # Two slots absorb the jitter of frames arriving back to back; beyond that
    # the oldest frame is dropped so latency never grows.
    QUEUE_DEPTH = 2

    def submit(self, jpeg):
        with self._cv:
            if len(self._frames) >= self.QUEUE_DEPTH:
                self._frames.popleft()
                self.dropped += 1
            self._frames.append(jpeg)
            self.frames_in += 1
            self._cv.notify()

    def update_settings(self, raw):
        with self._cv:
            new = clean_settings(raw, self.settings)
            if new != self.settings:
                self.settings = new
                self._restart = True
            return dict(self.settings)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify()
        self._thread.join(timeout=5)

    def _prepare_device(self):
        # A loopback device only becomes a capture device once the writer has
        # committed a frame of exactly the configured size, so pin the format
        # first (the same step DroidCam's client performs).
        for args in (
            ["-c", "keep_format=1"],
            [f"--set-fmt-video-out=width={self.width},height={self.height},pixelformat=YU12"],
        ):
            subprocess.run(["v4l2-ctl", "-d", self.device, *args],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def _output_args(self):
        if self.device == "null":
            return ["-f", "null", "-"]
        if self.device.startswith("file:"):
            return ["-f", "rawvideo", "-y", self.device[5:]]
        self._prepare_device()
        return ["-f", "v4l2", self.device]

    def _start_ffmpeg(self):
        vf = build_filter(self.settings, self.width, self.height)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error" if not self.verbose else "warning",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-probesize", "32", "-analyzeduration", "0",
            # Frames are forwarded as they arrive; the 120 fps input rate only
            # gives them distinct timestamps (the loopback ignores timing).
            "-framerate", "120",
            "-f", "image2pipe", "-c:v", "mjpeg", "-i", "-",
            "-vf", vf, "-fps_mode", "passthrough",
            *self._output_args(),
        ]
        if self.verbose:
            log("ffmpeg " + " ".join(cmd[1:]))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def _stop_ffmpeg(self):
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _run(self):
        while True:
            with self._cv:
                while not self._frames and not self._closed:
                    self._cv.wait()
                if self._closed:
                    break
                frame = self._frames.popleft()
                restart, self._restart = self._restart, False
            try:
                if restart:
                    self._stop_ffmpeg()
                if self.proc is None or self.proc.poll() is not None:
                    self._start_ffmpeg()
                self.proc.stdin.write(frame)
                self.proc.stdin.flush()
                self.frames_out += 1
            except (BrokenPipeError, OSError) as exc:
                log(f"video output failed ({exc}); restarting ffmpeg")
                self._stop_ffmpeg()
                time.sleep(0.2)
        self._stop_ffmpeg()


# --------------------------------------------------------------------------
# Virtual microphone (PulseAudio / PipeWire-pulse)
# --------------------------------------------------------------------------

class AudioPipe:
    SINK = "PirateCam"
    SOURCE = "PirateCamMic"

    def __init__(self):
        self.modules = []
        self.proc = None
        self.rate = None
        self._lock = threading.Lock()

    @staticmethod
    def available():
        return bool(shutil.which("pactl") and shutil.which("pacat"))

    @staticmethod
    def _pactl(*args):
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10)

    def _unload_stale(self):
        out = self._pactl("list", "short", "modules").stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and ("sink_name=" + self.SINK in parts[2] or "source_name=" + self.SOURCE in parts[2]):
                self._pactl("unload-module", parts[0])

    def setup(self):
        self._unload_stale()
        sink = self._pactl("load-module", "module-null-sink", f"sink_name={self.SINK}",
                           "sink_properties=device.description=PirateCam")
        if sink.returncode != 0:
            raise RuntimeError(sink.stderr.strip() or "could not create the virtual sink")
        self.modules.append(sink.stdout.strip())
        source = self._pactl("load-module", "module-remap-source", f"master={self.SINK}.monitor",
                             f"source_name={self.SOURCE}",
                             "source_properties=device.description=Pirate-Cam-Microphone")
        if source.returncode != 0:
            self.teardown()
            raise RuntimeError(source.stderr.strip() or "could not create the virtual microphone")
        self.modules.append(source.stdout.strip())

    def write(self, pcm, rate):
        with self._lock:
            if self.proc is not None and (self.rate != rate or self.proc.poll() is not None):
                self._stop_player()
            if self.proc is None:
                self.rate = rate
                self.proc = subprocess.Popen(
                    ["pacat", "--playback", f"--device={self.SINK}", f"--rate={rate}",
                     "--format=s16le", "--channels=1", "--latency-msec=40",
                     "--client-name=Pirate-Cam", "--stream-name=phone-microphone"],
                    stdin=subprocess.PIPE)
            try:
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._stop_player()

    def _stop_player(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def teardown(self):
        with self._lock:
            self._stop_player()
        while self.modules:
            self._pactl("unload-module", self.modules.pop())


# --------------------------------------------------------------------------
# WebSocket (RFC 6455, just what this app needs)
# --------------------------------------------------------------------------

class WsClosed(Exception):
    pass


def unmask(data, mask):
    n = len(data)
    if n == 0:
        return data
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class WsConn:
    def __init__(self, sock, rfile):
        self.sock = sock
        self.rfile = rfile
        self.audio_rate = 48000
        self._send_lock = threading.Lock()
        self.closed = threading.Event()

    def _read(self, n):
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise WsClosed()
        return data

    def recv(self):
        """Return (opcode, payload) for the next complete message."""
        message, opcode = bytearray(), None
        while True:
            b0, b1 = self._read(2)
            fin, op, masked, length = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if length > MAX_MESSAGE:
                raise WsClosed()
            mask = self._read(4) if masked else None
            payload = self._read(length) if length else b""
            if mask:
                payload = unmask(payload, mask)
            if op >= 8:
                if op == 8:
                    return 8, payload
                if op == 9:
                    self.send(10, payload)
                continue
            if op != 0:
                opcode = op
                message = bytearray(payload)
            else:
                message += payload
            if len(message) > MAX_MESSAGE:
                raise WsClosed()
            if fin:
                return opcode, bytes(message)

    def send(self, opcode, payload=b""):
        n = len(payload)
        if n < 126:
            header = struct.pack(">BB", 0x80 | opcode, n)
        elif n < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 126, n)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 127, n)
        with self._send_lock:
            self.sock.sendall(header + payload)

    def send_json(self, obj):
        self.send(1, json.dumps(obj).encode())

    def close(self):
        self.closed.set()
        try:
            self.send(8, struct.pack(">H", 1000))
        except OSError:
            pass


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class App:
    def __init__(self, sink, audio, pin):
        self.sink = sink
        self.audio = audio
        self.pin = pin
        self.active = None
        self._lock = threading.Lock()

    def attach(self, conn):
        """Make `conn` the one live phone; a newer connection replaces the old."""
        with self._lock:
            old, self.active = self.active, conn
        if old:
            old.close()

    def detach(self, conn):
        with self._lock:
            if self.active is conn:
                self.active = None

    def status(self):
        s = self.sink
        return {
            "version": VERSION, "device": s.device, "width": s.width, "height": s.height,
            "connected": self.active is not None, "frames_in": s.frames_in,
            "frames_out": s.frames_out, "dropped": s.dropped, "audio": self.audio is not None,
            "settings": s.settings,
        }


class TlsServer(ThreadingHTTPServer):
    """Threaded server whose TLS handshake runs in the request thread.

    Wrapping the listening socket would handshake inside accept(), so one
    stalled client (Safari opens speculative connections) blocks all others.
    """

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def get_request(self):
        sock, addr = self.socket.accept()
        if self.ctx is None:
            return sock, addr
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # Browsers abort the handshake while the user decides whether to trust
        # the self-signed certificate, and idle keep-alive connections time
        # out; neither deserves a traceback.
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        log(f"error handling a request from {client_address[0]}: {exc!r}")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _pin_ok(self, query):
            if not app.pin:
                return True
            given = (query.get("pin") or [""])[0]
            return hmac.compare_digest(given.encode(), app.pin.encode())

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                if not self._pin_ok(query):
                    return self._send(401, b"pin required")
                self._send(200, json.dumps(app.status()).encode(), "application/json")
            elif url.path == "/ws":
                self._websocket(query)
            elif url.path == "/favicon.ico":
                self._send(204)
            else:
                self._send(404)

        def do_HEAD(self):
            self.do_GET()

        def _websocket(self, query):
            key = self.headers.get("Sec-WebSocket-Key")
            if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                return self._send(400, b"websocket upgrade required")
            if not self._pin_ok(query):
                return self._send(401, b"pin required")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True

            conn = WsConn(self.connection, self.rfile)
            peer = self.client_address[0]
            app.attach(conn)
            log(f"phone connected from {peer}")
            sink = app.sink
            conn.send_json({
                "type": "hello", "version": VERSION, "out": {"w": sink.width, "h": sink.height},
                "audio": app.audio is not None, "settings": sink.settings,
            })
            threading.Thread(target=self._stats, args=(conn,), daemon=True).start()
            try:
                self.connection.settimeout(60)
                while not conn.closed.is_set():
                    opcode, payload = conn.recv()
                    if opcode == 8:
                        break
                    if opcode == 1:
                        self._control(conn, payload)
                    elif opcode == 2 and payload:
                        self._media(conn, payload)
            except (WsClosed, OSError, ssl.SSLError):
                pass
            finally:
                conn.closed.set()
                app.detach(conn)
                log(f"phone {peer} disconnected")

        def _media(self, conn, payload):
            kind, body = payload[0], payload[1:]
            if kind == KIND_VIDEO:
                if body[:2] == b"\xff\xd8" and len(body) > 100:
                    app.sink.submit(body)
                else:
                    app.sink.bad += 1
            elif kind == KIND_AUDIO and app.audio is not None:
                app.audio.write(body[: len(body) // 2 * 2], conn.audio_rate)

        def _control(self, conn, payload):
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "ping":
                conn.send_json({"type": "pong", "t": msg.get("t")})
            elif kind == "settings":
                applied = app.sink.update_settings(msg)
                conn.send_json({"type": "settings", "settings": applied})
            elif kind == "audio":
                rate = msg.get("rate")
                if _is_number(rate) and 8000 <= rate <= 96000:
                    conn.audio_rate = int(rate)

        def _stats(self, conn):
            sink = app.sink
            last_in, last_out, last = sink.frames_in, sink.frames_out, time.monotonic()
            while not conn.closed.wait(1.0):
                now = time.monotonic()
                dt = max(now - last, 1e-3)
                cur_in, cur_out = sink.frames_in, sink.frames_out
                try:
                    conn.send_json({
                        "type": "stats", "in_fps": round((cur_in - last_in) / dt, 1),
                        "out_fps": round((cur_out - last_out) / dt, 1),
                        "dropped": sink.dropped, "bad": sink.bad,
                    })
                except OSError:
                    return
                last_in, last_out, last = cur_in, cur_out, now

    return Handler


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------

def local_ips():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    return sorted(set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)))


def ensure_cert(cert_dir, ips):
    cert, key, stamp = cert_dir / "cert.pem", cert_dir / "key.pem", cert_dir / "ips.txt"
    want = ",".join(ips)
    if cert.exists() and key.exists() and stamp.exists() and stamp.read_text() == want:
        return cert, key
    cert_dir.mkdir(parents=True, exist_ok=True)
    san = ",".join([f"IP:{ip}" for ip in ips] + ["IP:127.0.0.1", "DNS:localhost"])
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "825", "-subj", "/CN=pirate-cam",
         "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True)
    key.chmod(0o600)
    stamp.write_text(want)
    return cert, key


def loopback_devices():
    """Return [(path, card)] for every v4l2loopback device we can write to."""
    found = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        info = subprocess.run(["v4l2-ctl", "-d", path, "--info"], capture_output=True, text=True).stdout
        driver = re.search(r"Driver name\s*:\s*(.+)", info)
        card = re.search(r"Card type\s*:\s*(.+)", info)
        if driver and "loopback" in driver.group(1).lower():
            found.append((path, card.group(1).strip() if card else "?", os.access(path, os.W_OK)))
    return found


def resolve_device(value):
    if value in ("null",) or value.startswith("file:"):
        return value
    if value != "auto":
        return value
    for path, _card, writable in loopback_devices():
        if writable:
            return path
    sys.exit("No writable v4l2loopback device found. Load the module, e.g.\n"
             "  sudo modprobe v4l2loopback exclusive_caps=1 card_label=Pirate-Cam\n"
             "or pass --device /dev/videoN.")


def preferred_ip(ips):
    for prefix in ("172.20.10.", "192.168.", "10.", "172."):
        for ip in ips:
            if ip.startswith(prefix):
                return ip
    return ips[0] if ips else "localhost"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Use your phone as a Linux webcam and microphone.")
    ap.add_argument("--device", default="auto",
                    help="v4l2loopback device, or 'auto' (default). 'null' and 'file:PATH' are for testing.")
    ap.add_argument("--size", default="1280x720", help="output size WxH (default 1280x720)")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--pin", default=None,
                    help="require this PIN to connect; 'auto' generates a random one")
    ap.add_argument("--audio", action="store_true",
                    help="create a virtual microphone (PulseAudio/PipeWire) fed by the phone")
    ap.add_argument("--list", action="store_true", help="list v4l2loopback devices and exit")
    ap.add_argument("--no-qr", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--cert-dir", default=None, help="where the self-signed certificate lives")
    ap.add_argument("--insecure-http", action="store_true",
                    help="serve plain HTTP (testing on localhost only; phones need HTTPS)")
    ap.add_argument("--version", action="version", version=f"pirate-cam {VERSION}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.list:
        devices = loopback_devices()
        if not devices:
            print("No v4l2loopback devices found.")
        for path, card, writable in devices:
            print(f"{path}  {card}  {'writable' if writable else 'NOT writable'}")
        return 0

    match = re.fullmatch(r"(\d{2,4})x(\d{2,4})", args.size.lower())
    if not match:
        sys.exit("--size must look like 1280x720")
    width, height = int(match.group(1)), int(match.group(2))
    if width % 2 or height % 2:
        sys.exit("--size must have even dimensions")
    for tool in ("ffmpeg", "v4l2-ctl", "openssl"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is required but was not found in PATH")

    device = resolve_device(args.device)
    pin = None
    if args.pin:
        pin = f"{secrets.randbelow(10000):04d}" if args.pin == "auto" else args.pin

    audio = None
    if args.audio:
        if not AudioPipe.available():
            sys.exit("--audio needs pactl and pacat (pulseaudio-utils / pipewire-pulse)")
        audio = AudioPipe()
        try:
            audio.setup()
        except (RuntimeError, subprocess.SubprocessError) as exc:
            sys.exit(f"could not create the virtual microphone: {exc}")

    sink = Sink(device, width, height, verbose=args.verbose)
    app = App(sink, audio, pin)

    ips = local_ips()
    ctx = None
    if not args.insecure_http:
        cert_dir = Path(args.cert_dir) if args.cert_dir else (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pirate-cam")
        cert, key = ensure_cert(cert_dir, ips)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    try:
        server = TlsServer((args.host, args.port), make_handler(app), ctx)
    except OSError as exc:
        sink.close()
        if audio:
            audio.teardown()
        sys.exit(f"cannot listen on port {args.port}: {exc.strerror}. Try --port with another number.")

    scheme = "http" if ctx is None else "https"
    suffix = f"/?pin={pin}" if pin else "/"
    log(f"Pirate-Cam {VERSION}: video -> {device} at {width}x{height}"
        + (", virtual microphone 'Pirate-Cam-Microphone'" if audio else ""))
    if pin:
        log(f"PIN: {pin}")
    log("Open one of these on the phone, then accept the certificate warning:")
    for ip in ips or ["localhost"]:
        log(f"  {scheme}://{ip}:{args.port}{suffix}")
    if not args.no_qr and shutil.which("qrencode") and ips:
        url = f"{scheme}://{preferred_ip(ips)}:{args.port}{suffix}"
        qr = subprocess.run(["qrencode", "-t", "ANSIUTF8", url], capture_output=True, text=True)
        print(qr.stdout, flush=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    # The receiver thread keeps the GIL while it handles each frame; a short
    # switch interval lets the writer thread wake promptly at 60 fps.
    sys.setswitchinterval(0.001)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("shutting down")
        server.server_close()
        sink.close()
        if audio:
            audio.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
