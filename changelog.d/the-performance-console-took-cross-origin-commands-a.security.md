- **The performance console took cross-origin commands.** A WebSocket handshake
  is exempt from CORS entirely, and Starlette's `Request.json()` never looks at
  `Content-Type` — so with `[control] enabled = true` and the unprompted default
  `token = ""`, any page the performer happened to visit could open
  `ws://127.0.0.1:8765/perf/ws`, read every pushed state frame, and send command
  frames that drove the running show; `POST /perf/command` was reachable the
  same way as a `text/plain` form submit, which is a CORS-simple request with no
  preflight to refuse. The open loopback mode is justified as "exposed to
  whoever already has a shell here", and a browser tab is not that person. Both
  `/perf/ws` and `/perf/command` — and `/api/ws`, which shares the loop — now
  refuse a request whose `Origin` is present and names a different host:port
  than its own `Host` (the handshake is closed before `accept`), and the POST
  requires an `application/json` content type. A request with **no** `Origin` is
  still served: that is `curl`, `wscat` or a script, which is exactly the caller
  the open mode describes.
