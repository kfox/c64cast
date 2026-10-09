- **Shutting the web console down left the C64 streaming its screen.** Once a
  browser had watched the picture, the Ultimate went on sending its VIC output
  — ~2.6 MB/s of UDP — at the host that had just exited, until the firmware's
  own 20-second watchdog stopped it. `ScreenFeed.close()` existed for exactly
  this and nothing called it; the host now runs it on the way down, before it
  releases the machines, so the stream ends with the process.
