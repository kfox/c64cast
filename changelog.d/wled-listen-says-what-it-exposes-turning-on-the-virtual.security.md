- **`[wled].listen` says what it exposes.** Turning on the virtual WLED device
  binds its JSON/WebSocket API on every interface with no token and advertises
  it over mDNS, and a reachable client can pause the run, jump scenes, sweep
  live params, force the palette and write presets — the same capability
  `validate_control_cfg` refuses to leave unauthenticated off loopback. LAN
  discovery is the point of the feature, so this warns rather than refuses, but
  it no longer happens silently.
