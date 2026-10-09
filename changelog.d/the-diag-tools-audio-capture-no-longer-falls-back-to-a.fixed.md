- **The diag tools' audio capture no longer falls back to a fixed device.**
  The ffmpeg tools recorded from avfoundation input `:3` whenever the Cam
  Link's name was not found, and `reu_audio_spectrum.py` always did; the
  sounddevice tools defaulted to input 1, and two of them fell back to the
  system default input, which on a laptop is its microphone. With no `-D` and
  no `C64_DIAG_AVF_AUDIO` / `C64_DIAG_SD_AUDIO`, a tool now records from the
  one audio input named like the capture camera (the tool's own `--device`
  where it has one, else the one `C64_DIAG_CAMERA` names, else the
  auto-picked HDMI capture device), and exits listing the
  inputs when none or several match. It looks before it touches the machine.
  `-D` takes an index or a name substring on every audio tool.
  `scripts/diags/vision_tune.py` no longer defaults to the camera named
  "FaceTime": with no `--device` it opens the one connected camera that does
  not look like an HDMI capture device, and exits listing the cameras when
  there is not exactly one.
