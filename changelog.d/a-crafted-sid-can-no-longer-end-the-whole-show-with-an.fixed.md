- **A crafted `.sid` can no longer end the whole show with an emulator
  crash.** The host emulator's memory refused an address past `$FFFF` instead
  of wrapping the way a real 6510's address bus does, and six bytes of 6502
  were enough to ask for one. The resulting error was not the kind the SID
  pool pickers catch, so it unwound past "log it and try the next candidate"
  and aborted the playlist. Addresses wrap, and anything else the interpreter
  raises is now reported as a non-terminating pass — the same verdict a tune
  that spins gets.
