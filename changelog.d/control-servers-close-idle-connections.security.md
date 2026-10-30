- **The web console, control plane and WLED device servers close connections
  that never finish a request.** Anyone on the network could open connections
  and send nothing, without a token, until c64cast ran out of file descriptors
  and could no longer reconnect to the Ultimate. A connection now has 10 s to
  send a request head, and may not go silent for 10 s partway through a body.
  Each server also accepts at most 64 open connections at once. Open console
  WebSockets are not affected.
