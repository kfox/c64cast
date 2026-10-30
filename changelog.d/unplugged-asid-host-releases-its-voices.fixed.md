- **Unplugging the MIDI interface feeding an ASID scene no longer leaves the SID
  droning.** The chip kept whatever notes the last frame had gated sounding
  until the scene ended; c64cast now notices the port is gone within about a
  second, warns, and gates every voice off. Plugged back in, or restarted under
  the same port name, the host is reconnected on its own, with no restart.
