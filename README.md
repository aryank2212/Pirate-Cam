# Pirate-Cam

Use your phone as a webcam (and microphone) on Linux. No app to install on the phone: it opens a web page, and your computer shows up as a normal camera in OBS, browsers, Zoom and anything else that reads V4L2.

```
phone browser --(WebSocket, JPEG frames + PCM audio)--> pirate_cam.py --> ffmpeg --> v4l2loopback camera
                                                                      \--> pacat --> virtual microphone
```

Python standard library only. Runtime tools: `ffmpeg`, `v4l2-ctl`, `openssl`; optional `qrencode` (QR code in the terminal) and `pactl`/`pacat` (microphone).

## Features

- **Up to 60 fps** at 360p to 1080p. Frames travel over one WebSocket; when the link falls behind, old frames are dropped instead of queued, so latency stays low.
- **Adaptive quality**: lowers the JPEG quality automatically when the connection is slow, and raises it again when it recovers.
- **Picture controls**: mirror, flip, rotate 0/90/180/270, letterbox or fill, brightness, contrast, saturation, black and white. Applied by ffmpeg on the computer; the phone preview shows an approximation.
- **Camera controls**: pick any camera the phone exposes, swap front and back, zoom and flashlight when the browser supports them.
- **Virtual microphone** (`--audio`): the phone's microphone appears as `Pirate-Cam-Microphone` and is removed when the server stops.
- **PIN** (`--pin 1234` or `--pin auto`) and a **QR code** that includes it.
- Auto-reconnect, keeps the phone screen awake, dim-screen button, hide-preview button, and a built-in test pattern for checking the pipeline without a camera.
- `GET /api/status` for debugging.

## Setup

### 1. Install the tools

Arch:

```bash
sudo pacman -S ffmpeg v4l-utils openssl qrencode v4l2loopback-dkms linux-headers
```

Debian / Ubuntu:

```bash
sudo apt install ffmpeg v4l-utils openssl qrencode pulseaudio-utils v4l2loopback-dkms
```

On other distributions install the same tools, and `v4l2loopback` from your package manager or from its [project page](https://github.com/v4l2loopback/v4l2loopback).

### 2. Create the virtual camera

`exclusive_caps=1` is what makes browsers and video-call apps list the device.

```bash
sudo modprobe v4l2loopback exclusive_caps=1 card_label="Pirate-Cam"
```

To keep it across reboots:

```bash
echo 'options v4l2loopback exclusive_caps=1 card_label="Pirate-Cam"' | sudo tee /etc/modprobe.d/pirate-cam.conf
echo v4l2loopback | sudo tee /etc/modules-load.d/pirate-cam.conf
```

Check it with `./pirate-cam --list`. If another program already owns a loopback device (a different webcam app, for example), close that program first or pass `--device /dev/videoN`.

### 3. Run it

```bash
./pirate-cam --audio --pin auto
```

It prints the addresses to open (and a QR code if `qrencode` is installed). On the phone:

1. Open the `https://` address. The certificate is self-signed and generated on first run, so accept the browser warning once.
2. Allow camera access, pick resolution and frame rate, tap **Start streaming**.
3. In OBS (Video Capture Device), a browser, or a call app, choose the Pirate-Cam camera.

Allow the port through your firewall for the phone's network, for example with ufw:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8443 proto tcp
```

## Getting a smooth stream

The network between phone and computer is usually the limit, not the software.

- **Use a good link.** A phone connected to a laptop's own 2.4 GHz hotspot can fall to a few Mbit/s upload and stutter even with a strong signal. Prefer your router's 5 GHz Wi-Fi, or a wired path.
- **USB tethering works well.** On an iPhone, enable *Personal Hotspot* and plug the phone in with a cable; the computer gets a `172.20.10.x` address and the round trip is around 1 ms. Android has an equivalent USB tethering switch. Open the page on the address the computer received.
- **60 fps** needs a phone and browser that deliver it at the chosen resolution, a link that can carry it, and a phone that can JPEG-encode that fast. The page shows the frame rate it actually sends. If it falls short, lower the resolution or quality, or leave *adaptive quality* on.
- In OBS, turn **Use Buffering** off on the Video Capture Device source; it adds its own delay.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--device` | `auto` | v4l2loopback device, or a path such as `/dev/video4` |
| `--size` | `1280x720` | output size on the computer (even numbers) |
| `--port` | `8443` | listening port |
| `--pin` | off | require a PIN; `auto` picks a random one |
| `--audio` | off | create the virtual microphone |
| `--list` | | list loopback devices (and whether you can write to them) and exit |
| `--no-qr` | | do not print a QR code |
| `--verbose` | | log the ffmpeg command line and ffmpeg warnings |
| `--cert-dir` | `~/.config/pirate-cam` | where the self-signed certificate is kept |
| `--insecure-http` | | plain HTTP, for testing on localhost only (phones need HTTPS for the camera) |

## Troubleshooting

- **The phone cannot open the page.** Check that it is on the same network, that the firewall allows the port, and try the other addresses the server prints.
- **The camera is not listed in a browser or call app.** Reload the module with `exclusive_caps=1` (step 2), and start streaming first; some apps only list the device once frames arrive.
- **A second program cannot open the camera ("Device or resource busy").** Loopback devices accept a limited set of readers; request the same format the first reader uses (`1280x720`, YUV 4:2:0).
- **"No writable v4l2loopback device found".** Load the module (step 2) or pass `--device`.
- **No microphone.** Start the server with `--audio`; it needs `pactl` and `pacat` and a PulseAudio or PipeWire session.
- **Picture changes cause a short pause.** They restart the ffmpeg process that feeds the camera.

## Security

Anyone who can reach the port can send video to your virtual camera unless a PIN is set. Run it on networks you trust, use `--pin`, and stop it when you are done. The connection is encrypted with a self-signed certificate, so the browser cannot verify the server's identity; the PIN is what authenticates the phone. The server listens on every network interface and makes no outgoing connections; restrict who can reach the port with your firewall.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests start real servers and talk to them over a WebSocket. They cover the protocol, the PIN, back-to-back phones, a 60 fps run (300 frames, checked for loss), and that mirror, rotate and black-and-white really change the pixels. They write to a file or `null`, so no loopback device is needed.

Not covered by automated tests: the phone's own camera, zoom and flashlight, and the microphone capture in the browser. The audio path from the WebSocket to the virtual microphone was checked by hand with a test tone.

## License

MIT. See [LICENSE](LICENSE).
