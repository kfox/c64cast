---
number: I
generated: true
---

# Optional Extras

The 13 groups of dependency that a plain install leaves out, what each one unlocks, the module `c64cast --doctor` imports to tell you it is there, and the packages it brings with it.

## The Extras

Extras do not accumulate. Installing `c64cast[midi]` over `c64cast[video]` leaves you with MIDI and no video, so the install worth asking for is `c64cast[all]` — or `uv sync --all-extras` from a checkout. `c64cast --doctor` says which of these are importable and which are missing.

<!-- table: fields -->
| Extra | Description |
|---|---|
| **`camera`**<br>`cv2_enumerate_cameras` | [video].device by name/VID:PID; `--list-devices` detail. `cv2-enumerate-cameras>=1.3.3,<2`. |
| **`control`**<br>`fastapi` | [control] enabled HTTP plane. `fastapi>=0.140.0,<1`, `uvicorn>=0.51.0,<1`. |
| **`logging`**<br>`rich` | colored log output. `rich>=15.0.0,<16`. |
| **`mic`**<br>`sounddevice` | [audio] enabled, mic capture. `sounddevice>=0.5.5,<0.6`. |
| **`midi`**<br>`mido` | midi scenes; [midi_control] live control. `mido>=1.3.3,<2`, `python-rtmidi>=1.5.8,<2`. |
| **`obs`**<br>`obsws_python` | obs_status overlay. `obsws-python>=1.8.0,<2`. |
| **`tr`**<br>`serial` | TeensyROM serial backend. `pyserial>=3.5,<4`. |
| **`video`**<br>`av` | video scenes, video interleaving. `av>=18.0.0,<19`. |
| **`vision`**<br>`mediapipe` | [vision] enabled gesture control. `mediapipe>=0.10.35,<1.1`. |
| **`web`**<br>`websockets` | `--serve` web console host. `fastapi>=0.140.0,<1`, `uvicorn>=0.51.0,<1`, `websockets>=16.1.1,<18`. |
| **`wizard`**<br>`questionary` | `--init` config wizard. `questionary>=2.1.1,<3`. |
| **`wled`**<br>`zeroconf` | [wled].listen virtual WLED device. `zeroconf>=0.150.0,<1`, `fastapi>=0.140.0,<1`, `uvicorn>=0.51.0,<1`, `websockets>=16.1.1,<18`. |
| **`yt`**<br>`yt_dlp` | cast URL playback (YouTube et al.). `yt-dlp>=2026.7.4`. |
