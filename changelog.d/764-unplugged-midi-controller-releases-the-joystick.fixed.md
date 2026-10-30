- **Unplugging a MIDI controller no longer leaves a joystick direction held on
  the C64.** A pad mapped to `joystick` that was down when the controller was
  unplugged used to hold that direction until the program ended; c64cast now
  notices the port is gone within about a second, warns, and releases it.
  Plugged back in, a USB controller on macOS plays on as before; if its pads
  stay dead, restart c64cast.
