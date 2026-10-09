- **On Ultimate firmware 3.15, the first write after a pause of a second or
  more no longer goes missing.** Firmware 3.15 closes a DMA connection that has
  sent it nothing for one second, and the next write went into the closed
  connection without an error, so a still picture with audio off, or the
  first write of a scene after start-up, could lose a write that the cache
  of unchanged bytes then never sent again; a flush after such a pause
  failed with "socket closed mid-read". Before each command c64cast now checks whether the
  Ultimate has closed the connection, and after a pause it confirms the
  connection with one round trip; a closed one is reopened first, which costs
  about 7 ms. Firmware without the timeout (3.14 and earlier, C64 Ultimate
  1.1.0) keeps its connection and pays only that round trip after a pause. The
  `--profile` latency line now ends with a `reconnects=` count.
