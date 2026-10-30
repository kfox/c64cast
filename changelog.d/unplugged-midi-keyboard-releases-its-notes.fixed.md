- **Unplugging the keyboard driving a MIDI scene no longer leaves a note
  sounding.** A key held when the controller was unplugged kept its SID voice
  playing until the scene ended; c64cast now notices the port is gone within
  about a second, warns, and releases every voice that was sounding. Plugged
  back in, the keyboard is reconnected on its own and plays on, with no
  restart.
