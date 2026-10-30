- **The cap on open console sockets holds against a burst of connections.**
  `/perf/ws` and `/api/ws` checked the cap before a socket was counted, so
  many handshakes arriving together were all let in. Each is now counted
  before it is accepted.
