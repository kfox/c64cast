- `-u`/`--url`/`$C64CAST_URL` accepted a `user:pass@` netloc (`u64://admin:s3cret@host`)
  and carried it verbatim into `[ultimate64].url` — from which `requests`
  sent it as an HTTP Basic-auth header on every REST call to a device that
  has no HTTP auth of its own, and `--save-settings` both wrote it into
  `settings.toml` and echoed it to stdout in plaintext, directly undercutting
  this project's "the DMA password is env/config-only, never a CLI flag"
  posture for anyone who assumed the URL was where a credential went.
  `connect.parse_connection_uri` now refuses any target carrying userinfo, on
  every scheme, naming `C64CAST_DMA_PASSWORD`/`[ultimate64].dma_password` as
  the place a secret actually belongs. Related connect.py hardening in the
  same pass: the `http(s)://` branch passed the whole target (including its
  `?query` string) through as the base URL while *also* consuming
  `dma_port` out of that same query, so `-u 'http://host?dma_port=64'` left
  `?dma_port=64` inside the string `Ultimate64API` concatenates every REST
  path onto — it now rebuilds the URL from its parts like the `u64://`
  branch already did. A netloc port is now validated the same way on every
  scheme (`u64://host:badport` and `http://host:badport` used to parse
  cleanly into a URL `requests` would only reject deep in the startup probe,
  misdiagnosing as "could not reach the hardware"); `tr://host:2113?tcp_port=x`
  used to skip validating the query param entirely because `port or
  _int_query(...)` only reached the query when the netloc had no port of its
  own (the same typo raised on `tr://host?tcp_port=x` but was silently
  ignored on `tr://host:2113?tcp_port=x`); and an unrecognized or blank
  `?query` key (`?dmaport=64`, `?dma_port=`) is now rejected instead of
  silently parsed as absent, matching the strictness a TOML config already
  gets.
