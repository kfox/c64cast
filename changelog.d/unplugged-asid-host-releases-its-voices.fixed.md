- **Unplugging the MIDI interface feeding an ASID scene no longer leaves the SID
  droning.** The chip kept whatever notes the last frame had gated sounding
  until the scene ended; c64cast now notices the port is gone within about a
  second, warns, and gates every voice off. Plug the interface back in and
  restart c64cast to use it again.
