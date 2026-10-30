- **The web console, control plane and WLED device servers close connections
  that never finish a request.** Anyone on the network could open connections
  and send nothing, without a token, until c64cast ran out of file descriptors
  and could no longer reconnect to the Ultimate. A connection now has 10 s to
  send a request head, and may not go silent for 10 s partway through a body.
  A body still arriving after the server has refused the request, for example
  with a 401 for a missing token, is cut off 10 s later however fast it
  trickles in. Each server also accepts at most 64 open connections at once. Open console
  WebSockets are not affected.
