- **Stopping a show left the C64 streaming its screen for 20 seconds.** With a
  browser watching the picture, stopping the show from the web console left the
  Ultimate sending its VIC output — ~2.6 MB/s of UDP — until the firmware's own
  watchdog expired. The OFF command *was* attempted, and reached a link the
  teardown had already closed, where the resulting error was swallowed with
  nothing logged. The machine is now told while the link is still up, as the
  last teardown step before it closes, and it is told whether or
  not this host believes anyone is still watching. Sibling of the
  host-shutdown case below, on a different path and not fixed by it.
