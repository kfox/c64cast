- **Unplugging a MIDI controller no longer leaves a joystick direction held on
  the C64.** A pad mapped to `joystick` that was down when the controller was
  unplugged used to hold that direction until the program ended; c64cast now
  notices the port is gone within about a second, warns, and releases it.
  Plug the controller back in and restart c64cast to use it again.
