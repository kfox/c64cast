- **A bare host in `[ultimate64].url` skipped URI validation entirely.** A
  value with no `://` was prefixed with `http://` and returned without ever
  reaching `connect.parse_connection_uri`, so `url =
  "admin:hunter2@192.168.2.64"` was accepted verbatim and handed to
  `requests` as Basic auth — the same `user:pass@` refusal that the
  scheme-carrying spelling gets, reached from a different door. The value is
  normalized first and validated always, which also closes the sibling
  bypass: `url = "192.168.2.64?dma_port=9999"` was passing a query param
  straight into the base URL that this field's own help says cannot carry
  one.
