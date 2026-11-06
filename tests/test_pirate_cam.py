"""Integration tests: run the real server and talk to it over a real WebSocket.

Needs ffmpeg and v4l2-ctl. Output goes to `null` or a raw file, so no
loopback device is required. Run with:  python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pirate_cam  # noqa: E402

W, H = 1280, 720
FRAME = W * H * 3 // 2

needs_tools = unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("v4l2-ctl") and shutil.which("openssl"),
    "ffmpeg, v4l2-ctl and openssl are required")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_jpeg(path, graph, size):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph,
                    "-frames:v", "1", "-s", size, "-q:v", "5", str(path)], check=True)
    return Path(path).read_bytes()


class WsClient:
    """Tiny RFC 6455 client (masked frames) over plain TCP."""

    def __init__(self, port, query=""):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /ws{query} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.head, _, self.rest = head.partition(b"\r\n\r\n")
        self.status = int(self.head.split(b" ")[1])
        self.buf = self.rest

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv(self):
        b0, b1 = self._read(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        return b0 & 0x0F, self._read(length)

    def recv_json(self, kind=None, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            op, payload = self.recv()
            if op == 1:
                msg = json.loads(payload)
                if kind is None or msg.get("type") == kind:
                    return msg
        raise TimeoutError(kind)

    def send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = pirate_cam.unmask(payload, mask)  # XOR is its own inverse
        self.sock.sendall(header + mask + masked)

    def send_json(self, obj):
        self.send(1, json.dumps(obj).encode())

    def send_frame(self, jpeg):
        self.send(2, bytes([pirate_cam.KIND_VIDEO]) + jpeg)

    def close(self):
        self.sock.close()


class Server:
    def __init__(self, *extra, device="null", tls=False):
        self.port = free_port()
        # Never use an undrained PIPE here: a full pipe blocks the server's ffmpeg.
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pirate_cam.py"), *([] if tls else ["--insecure-http"]), "--no-qr",
             "--port", str(self.port), "--device", device, "--size", f"{W}x{H}", *extra],
            stdout=self.log, stderr=subprocess.STDOUT)
        end = time.time() + 15
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not start")

    def stop(self, keep_log=False):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if not keep_log:
            self.log.close()

    def read_log(self):
        """Everything the server and its ffmpeg printed; stop(keep_log=True) first."""
        self.log.seek(0)
        text = self.log.read().decode(errors="replace")
        self.log.close()
        return text


def http_get(port, path):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


class PureLogic(unittest.TestCase):
    def test_clean_settings_clamps_and_rejects_junk(self):
        cur = dict(pirate_cam.DEFAULT_SETTINGS)
        out = pirate_cam.clean_settings({"brightness": 9, "contrast": -4, "saturation": "x", "mirror": 1,
                                         "rotate": 45, "fit": "zoom", "gray": True}, cur)
        self.assertEqual(out["brightness"], 1.0)
        self.assertEqual(out["contrast"], 0.0)
        self.assertEqual(out["saturation"], 1.0)
        self.assertFalse(out["mirror"])
        self.assertEqual(out["rotate"], 0)
        self.assertEqual(out["fit"], "pad")
        self.assertTrue(out["gray"])
        self.assertEqual(pirate_cam.clean_settings({"rotate": 270, "fit": "fill"}, cur)["rotate"], 270)
        self.assertEqual(pirate_cam.clean_settings({"rotate": True}, cur)["rotate"], 0)

    def test_filter_chain(self):
        s = dict(pirate_cam.DEFAULT_SETTINGS)
        base = pirate_cam.build_filter(s, 1280, 720)
        self.assertTrue(base.startswith("scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720"))
        self.assertTrue(base.endswith("format=yuv420p"))
        s.update(rotate=90, mirror=True, flip=True, gray=True, brightness=0.2, fit="fill")
        chain = pirate_cam.build_filter(s, 640, 480)
        for part in ("transpose=1", "hflip", "vflip", "hue=s=0", "eq=brightness=0.200", "crop=640:480"):
            self.assertIn(part, chain)
        self.assertNotIn("pad=", chain)

    def test_unmask_roundtrip(self):
        data = os.urandom(1001)
        mask = os.urandom(4)
        self.assertEqual(pirate_cam.unmask(pirate_cam.unmask(data, mask), mask), data)
        self.assertEqual(pirate_cam.unmask(b"", mask), b"")


@needs_tools
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pirate-cam-test-"))
        cls.jpeg = make_jpeg(cls.tmp / "bars.jpg", "testsrc=size=1280x720", "1280x720")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_tls_serves_and_rejected_handshakes_are_quiet(self):
        srv = Server("--cert-dir", str(self.tmp / "certs"), tls=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            # Safari finishes the handshake, then drops the connection while the
            # user decides whether to trust the self-signed certificate.
            for _ in range(3):
                tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", srv.port), timeout=5),
                                      server_hostname="localhost")
                tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                tls.close()
            time.sleep(0.5)
            with urllib.request.urlopen(f"https://127.0.0.1:{srv.port}/api/status", context=ctx, timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(json.load(r)["width"], W)
        finally:
            srv.stop(keep_log=True)
        log = srv.read_log()
        self.assertNotIn("Traceback", log)
        self.assertNotIn("error handling", log)

    def test_page_and_status(self):
        srv = Server()
        try:
            code, body = http_get(srv.port, "/")
            self.assertEqual(code, 200)
            self.assertIn(b"Pirate-Cam", body)
            code, body = http_get(srv.port, "/api/status")
            self.assertEqual(code, 200)
            status = json.loads(body)
            self.assertEqual((status["width"], status["height"]), (W, H))
            self.assertEqual(http_get(srv.port, "/nope")[0], 404)
        finally:
            srv.stop()

    def test_websocket_hello_ping_and_frames_reach_output_file(self):
        out = self.tmp / "out.yuv"
        srv = Server(device=f"file:{out}")
        try:
            ws = WsClient(srv.port)
            self.assertEqual(ws.status, 101)
            hello = ws.recv_json("hello")
            self.assertEqual(hello["out"], {"w": W, "h": H})
            self.assertFalse(hello["audio"])
            ws.send_json({"type": "ping", "t": 123})
            self.assertEqual(ws.recv_json("pong")["t"], 123)
            for _ in range(20):
                ws.send_frame(self.jpeg)
                time.sleep(0.03)
            ws.close()
        finally:
            srv.stop()
        size = out.stat().st_size
        self.assertEqual(size % FRAME, 0)
        self.assertGreaterEqual(size // FRAME, 10)

    def test_pin_is_enforced(self):
        srv = Server("--pin", "4321")
        try:
            for query in ("", "?pin=0000"):
                rejected = WsClient(srv.port, query)
                self.assertEqual(rejected.status, 401)
                rejected.close()
            ok = WsClient(srv.port, "?pin=4321")
            self.assertEqual(ok.status, 101)
            ok.close()
            self.assertEqual(http_get(srv.port, "/api/status")[0], 401)
            self.assertEqual(http_get(srv.port, "/api/status?pin=4321")[0], 200)
        finally:
            srv.stop()

    def test_newer_phone_replaces_older(self):
        srv = Server()
        try:
            first = WsClient(srv.port)
            first.recv_json("hello")
            second = WsClient(srv.port)
            second.recv_json("hello")
            op, _ = first.recv()
            while op != 8:
                op, _ = first.recv()
            self.assertEqual(op, 8)
            first.close()
            second.close()
        finally:
            srv.stop()

    def test_sixty_fps_is_ingested(self):
        srv = Server()
        try:
            ws = WsClient(srv.port)
            ws.recv_json("hello")
            frames, started = 300, time.monotonic()
            for i in range(frames):
                ws.send_frame(self.jpeg)
                delay = started + (i + 1) / 60 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            elapsed = time.monotonic() - started
            time.sleep(0.5)
            status = json.loads(http_get(srv.port, "/api/status")[1])
            ws.close()
        finally:
            srv.stop(keep_log=True)
        self.assertNotIn("non monotonically", srv.read_log())
        self.assertGreater(elapsed, 4.5)
        self.assertEqual(status["frames_in"], frames)
        self.assertGreaterEqual(status["frames_out"], frames * 0.95,
                                f"pipeline kept up with only {status['frames_out']}/{frames} frames")

    def test_bad_frames_are_counted_not_forwarded(self):
        srv = Server()
        try:
            ws = WsClient(srv.port)
            ws.recv_json("hello")
            ws.send(2, bytes([pirate_cam.KIND_VIDEO]) + b"not a jpeg" * 20)
            ws.send(2, b"")
            ws.send(1, b"{not json")
            stats = ws.recv_json("stats", timeout=5)
            self.assertEqual(stats["bad"], 1)
            self.assertEqual(json.loads(http_get(srv.port, "/api/status")[1])["frames_in"], 0)
            ws.close()
        finally:
            srv.stop()

    def _render(self, settings, jpeg, count=8):
        out = self.tmp / f"render-{abs(hash(json.dumps(settings, sort_keys=True)))}.yuv"
        srv = Server(device=f"file:{out}")
        try:
            ws = WsClient(srv.port)
            ws.recv_json("hello")
            if settings:
                ws.send_json({"type": "settings", **settings})
                ws.recv_json("settings")
            for _ in range(count):
                ws.send_frame(jpeg)
                time.sleep(0.05)
            time.sleep(0.3)
            ws.close()
        finally:
            srv.stop()
        data = out.read_bytes()
        self.assertGreaterEqual(len(data) // FRAME, 1)
        last = data[(len(data) // FRAME - 1) * FRAME:]
        return last[: W * H], last[W * H: W * H + W * H // 4], last[W * H + W * H // 4:]

    def test_grayscale_removes_colour(self):
        _, u, v = self._render({"gray": True}, self.jpeg)
        self.assertLess(max(abs(b - 128) for b in u), 3)
        self.assertLess(max(abs(b - 128) for b in v), 3)
        _, u, v = self._render({}, self.jpeg)
        self.assertGreater(max(abs(b - 128) for b in u), 20)

    def test_mirror_swaps_left_and_right(self):
        split = make_jpeg(self.tmp / "split.jpg",
                          "color=c=white:s=640x720[l];color=c=black:s=640x720[r];[l][r]hstack", "1280x720")
        y, _, _ = self._render({}, split)
        self.assertGreater(y[360 * W + 100], 200)
        self.assertLess(y[360 * W + 1180], 40)
        y, _, _ = self._render({"mirror": True}, split)
        self.assertLess(y[360 * W + 100], 40)
        self.assertGreater(y[360 * W + 1180], 200)

    def test_rotate_90_letterboxes_into_output_size(self):
        wide = make_jpeg(self.tmp / "white.jpg", "color=c=white:s=1280x720", "1280x720")
        y, _, _ = self._render({"rotate": 90}, wide)
        self.assertLess(y[360 * W + 10], 40)        # pillar box on the left
        self.assertGreater(y[360 * W + W // 2], 200)  # image in the middle
        self.assertLess(y[360 * W + W - 10], 40)    # pillar box on the right

    def test_settings_change_restarts_ffmpeg_and_frames_keep_flowing(self):
        out = self.tmp / "restart.yuv"
        srv = Server(device=f"file:{out}")
        try:
            ws = WsClient(srv.port)
            ws.recv_json("hello")
            for _ in range(5):
                ws.send_frame(self.jpeg)
                time.sleep(0.04)
            ws.send_json({"type": "settings", "brightness": 0.3})
            ws.recv_json("settings")
            for _ in range(5):
                ws.send_frame(self.jpeg)
                time.sleep(0.04)
            time.sleep(0.3)
            status = json.loads(http_get(srv.port, "/api/status")[1])
            ws.close()
        finally:
            srv.stop()
        self.assertEqual(status["settings"]["brightness"], 0.3)
        self.assertGreaterEqual(status["frames_out"], 5)


if __name__ == "__main__":
    unittest.main()
