- **The diag and capture tools pick the HDMI capture device themselves, and
  stop rather than open another camera.** With no device flag they opened cv2
  index 0. When a U64 PAL/NTSC switch dropped the Cam Link off the USB bus,
  the laptop's own camera moved to index 0 and the tools filmed the room
  instead of the C64. With no `--device` and no `C64_DIAG_CAMERA`, a tool now
  opens the one connected camera that looks like an HDMI capture device: a USB
  device whose name matches no webcam, phone or virtual-camera pattern. With
  none or several, it exits listing every camera's name and VID:PID rather
  than opening one. Every tool in
  `scripts/diags/` that captures video, and `scripts/capture_guide_figure.py`,
  now takes the same `--device` (an index, a name substring, or a VID:PID);
  the older `--index`, `--cv2-index` and `--cam` still work.
  `C64_DIAG_CAMERA` takes any of those forms.
