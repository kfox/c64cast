- **The control plane's transport verbs took cross-origin commands too.**
  `POST /pause`, `/resume`, `/skip` and `/reload` take only a query param and no
  body, so a cross-site form POST at one is a CORS-simple request with no
  preflight to refuse — and with `[control] enabled = true` and the unprompted
  default `token = ""`, any page the performer happened to visit could pause,
  resume, skip or reload the running show. The console's own POST was fixed
  above; these four predate that check and now share it. A request with **no**
  `Origin` is still served, so `curl`, scripts and Home Assistant are
  unaffected — only a cross-origin browser is refused, and no browser client
  for these routes exists.
