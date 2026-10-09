- `POST /api/login` and `POST /api/setup` are both reachable with no
  credential and both called `await request.json()`, which buffers a body of
  any size — a remote memory exhaustion on a 1–2 GB appliance, taking down a
  process that owns live hardware. Both now read through a shared capped
  reader (`Content-Length` refused up front, then the stream abandoned past
  the cap, which is the only check a chunked body cannot lie about) and
  answer `413`. `web_api`'s own body reader shares it, so `ConfigStore`'s
  `ConfigTooLarge` — which protects the *file* — stops being the only limit.
