- **`[wled].listen` bound a tokenless control surface to the network by
  default, and only warned about it.** Mode 1 covers everything the control
  plane's four verbs do and more — `on=false` pauses, `seg[].fx` jumps scenes,
  `sx`/`ix` sweep live params, `pal`/`col` force the palette, a preset save
  writes the data dir — while carrying no token at all and being advertised over
  mDNS, so any host on the segment could drive the show. Yet `[control]`, whose
  whole surface is pause/resume/skip/reload, *refuses* to bind off loopback
  without a credential, and this only logged a warning. It now refuses the same
  way, with `[wled].allow_unauthenticated = true` as the opt-in for a network
  you trust — a config flag rather than a token because the WLED protocol has
  none to offer and LAN discovery is the entire feature. **This is a breaking
  change for an existing `listen` config:** Mode 1's default endpoint is
  `0.0.0.0:8080`, so a plain `listen = "enabled"` now needs either the opt-in or
  a loopback bind (`listen = "127.0.0.1:8080"`).
