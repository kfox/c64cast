- **The `network` overlay's `ping` line reads `PING -` on a TeensyROM.** With no
  REST host to reach it pinged the empty host name instead, which showed either
  a localhost round trip or `...` for the whole run.
