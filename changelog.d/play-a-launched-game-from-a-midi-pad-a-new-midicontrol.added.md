- **Play a launched game from a MIDI pad.** A new `[midi_control]` action,
  `joystick`, holds a joystick direction or the fire button (`input`, on
  `port` 1 or 2) for as long as its note is down. It drives the program a
  `launcher` scene is running. It uses the keyboard and joystick input that
  Ultimate 64 firmware **3.15** added. Older firmware, the C64 Ultimate on
  1.1.0, the Ultimate II+ and the TeensyROM drop the input, with a log line
  saying why. `scripts/diags/rest_input_probe.py` checks typing and joystick
  input on a real machine.
