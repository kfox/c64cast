- `_redact.py`'s pattern matched only a literal `token=` immediately followed
  by the value — the shape of today's console login-URL log line, but not
  `token = "…"` (spaces, as a TOML/config rendering would produce), `"token":
  "…"` (JSON), or an `Authorization: Bearer …` header, any of which could put
  the console's admin token into `--log-file` or a viewer's `SessionLogBuffer`
  tail in a future rendering with no test catching it. The pattern now covers
  `token`/`password`/`secret`/`api[_-]key` with `=` or `:`, quoted or not,
  plus a `Bearer <value>` alternative.
