- The WLED sink no longer re-copies the whole frame buffer on every single
  incoming datagram — publishing is now rate-limited to a real display frame
  budget (1/60s), closing off a minimal-datagram flood as a way to peg a
  core in memcpy. New `[[scenes]]` fields for `type = "wled"`: `sink_allow`
  restricts accepted senders to an IP allowlist (neither wire protocol
  authenticates, so this is the only barrier against another LAN host
  injecting frames into the broadcast), and `sink_ddp_port` / `sink_wled_port`
  move the sink off its two standard ports if something else on the host
  already owns one. The receiver also now logs the first accepted datagram's
  source, and the first datagram a parser rejects, so a silent "nothing on
  screen" is attributable.
