- **`--calibrate-dac` no longer records from the system default input.**
  Without `--audio-device`, it used to take the first input whose name looked
  like a capture device, else the system default input — on a laptop the
  microphone, so the run measured room noise for about a minute and then
  failed. It now records from the one audio input named like the connected
  HDMI capture device, which it finds with the `camera` extra. When it cannot
  single one out, it stops before touching the machine, lists the inputs, and
  asks for `--audio-device`. An `--audio-device` name that matches no input, or
  more than one, now stops the run too, instead of falling back to the default
  input (#568).
