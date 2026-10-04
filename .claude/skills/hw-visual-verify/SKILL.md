---
name: hw-visual-verify
description: Visually verify a rendered change on real C64 hardware (U64/U2+/TeensyROM) by capturing the HDMI output through a USB capture device. Use when confirming overlays, display modes, palette/dither/color changes, or scene transitions actually render correctly — RAM dumps alone cannot prove what the VIC drew.
---

# Visual verification on real hardware

The U64's HTTP API lets you confirm *what was written* to screen / color RAM / VIC
registers (`/v1/machine:readmem`), but it can't tell you *what the VIC actually
rendered* — character-ROM mismatches, MCM bit-3 surprises, and mode-switch
artifacts only show up on the screen itself. When you need that ground truth and
a USB video capture device is wired to the U64's HDMI output (e.g. Elgato Cam
Link, AverMedia, any UVC capture stick), `cv2.VideoCapture(index)` will return
a 1080p BGR frame you can `imwrite()` and Read.

Don't write a capture script from scratch — the committed tooling covers both
shapes of the job:

- [scripts/diags/hdmi_capture.py](../../../scripts/diags/hdmi_capture.py) grabs
  still frame(s) from the capture device (`-n`/`--delay` for a sequence,
  `--full` for native 1080p pixel-peeking; it discards warm-up frames and
  prints the written paths).
- [scripts/diags/run_and_capture.py](../../../scripts/diags/run_and_capture.py)
  is the full launch–capture–reset harness: it starts audio capture *before*
  c64cast (so the boot window isn't missed), grabs frames across the run, and
  resets the machine on exit.

Improve these rather than writing throwaway variants.

Run every command that touches the machine or the capture device under
[scripts/diags/hw_lock.py](../../../scripts/diags/hw_lock.py)
(`python3 scripts/diags/hw_lock.py uv run scripts/diags/hdmi_capture.py …`): another
session may be driving the same rig, and the DMA service and the capture device
each take one user at a time. Keep each locked command short.

**Ask the user before assuming a capture is available** — they vary by machine. If
one is present, use it for verification of any visual change (overlays, display
modes, scene transitions) instead of guessing from RAM dumps alone.

## Finding the capture device

`c64cast --list-devices` shows each camera's name + USB VID:PID + correct index
when the `camera` extra (cv2-enumerate-cameras) is installed — so the Cam Link is
identifiable by its Elgato VID rather than by trial-and-error index probing.

`[video].device` also accepts a name substring or `VID:PID` string (resolved via
[camera.py](../../../c64cast/control/camera.py) `resolve_camera_index`), so a
webcam scene can target the capture stick stably.

Every capture tool takes the same three forms on `--device` (`-d` on
`hdmi_capture.py` and `run_and_capture.py`) — index, name substring, or
`VID:PID` — through the same resolver. Leave it out: the default is the Cam
Link's `0fd9:0066`, which survives index renumbering, and a tool whose Cam Link
is absent exits rather than open another camera — the laptop's own camera is
one renumbering away from index 0. Never pass an index to find out which camera
is which; read `c64cast --list-devices` instead. `$C64_DIAG_CAMERA` sets the
default for a shell; an index-only `$C64_DIAG_CV2` still works, with a warning.

## When a capture returns no frame

`hdmi_capture.py` and the other tools retry a no-frame read for a few seconds
before failing, so a failure means the device gave nothing for that whole
window. It does not mean the capture stick is dead. Check for these first:

- **The HDMI link is renegotiating.** After a video-mode change on the
  machine (PAL/NTSC), the Cam Link has returned no frames for anything from
  seconds to over a minute, and has dropped off the device list for part of
  that. Wait, then rerun; a reset or a reboot has not been seen to do this.
  While the stick is off the list, the next camera is renumbered into its
  index, which is why you select it by name (`-d "Cam Link"`), never by
  index.
- **The source sends no signal**, or another program holds the device.

`c64cast --list-devices` shows whether the stick is listed at all.

## Scope

Local-only machine specifics (which OpenCV index is the capture device on this
host, what else is on the LAN) belong in `.claude/settings.local.json` or
auto-memory, **not** in a checked-in file.
