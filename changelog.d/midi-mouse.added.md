- **A MIDI XY pad can drive a mouse in a program a launcher scene runs.** Two
  new `[midi_control]` actions reach the mouse a C64 Ultimate on firmware
  1.2.1 or newer drives on control port 1: `mouse_move` takes one axis of an
  XY pad (`axis = "x"` or `"y"`, with optional `scale` and `invert`) and moves
  the pointer like a trackpad, by how far the finger moved, without flinging
  it when the finger lifts and lands elsewhere; `mouse_button` holds `left`,
  `right` or `middle` while a pad is down. A `joystick` mapping on port 1 next
  to a mouse action is refused at load, since both would drive port 1.
