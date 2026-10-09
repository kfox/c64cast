- **If `--calibrate-dac` used to find your capture input on its own, it may
  now ask for `--audio-device`.** It finds the input through the HDMI capture
  device, which needs the `camera` extra (`c64cast[all]` includes it). If your
  C64's audio arrives on a line-in rather than a capture stick, pass that input
  with `--audio-device`.
