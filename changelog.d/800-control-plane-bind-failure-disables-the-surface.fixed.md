- **A `[control]` or `[wled].listen` port already in use now disables that
  surface by name.** The run used to carry on holding a server that never
  bound, and the WLED device went on advertising the dead port over mDNS, so
  the WLED app discovered a device nothing answered for. The log now says
  "control plane disabled" or "WLED device disabled" with the address, and the
  WLED device is not advertised.
