- **Unplugging the keyboard driving a MIDI scene no longer leaves a note
  sounding.** A key held when the controller was unplugged kept its SID voice
  playing until the scene ended; c64cast now notices the port is gone within
  about a second, warns, and releases every voice that was sounding. Plug the
  controller back in and restart c64cast to use it again.
