- **The WLED Mode 1 websocket now refuses a cross-origin handshake.**
  `POST /json` has always rejected one, but `/ws` — which applies the same
  commands — accepted before checking anything. A WebSocket handshake is
  exempt from CORS entirely, so no preflight stood in the way: any page the
  operator happened to visit could open `ws://<host>:8080/ws` and pause the
  run, jump scenes, sweep live params or write presets. Binding to loopback
  was no defense, since that is the origin such a page reaches most easily.
  The socket is now closed before `accept`, so the handshake fails as an HTTP
  403 rather than as an indistinguishable disconnect, and the check is the
  same `auth.same_origin` the control plane and the `/perf` console use — the
  bridge's own divergent copy of the comparison is gone.
