- **A quoted secret was redacted down to its first word, or not at all.**
  `redact_secrets` accepted only a double quote around the value and ended the
  value at the first space, so `dma_password = "correct horse battery staple"`
  came back as `"REDACTED horse battery staple"`, a TOML literal string
  (`dma_password = 'hunter2'`) or a Python mapping `repr()`
  (`{'token': 's3cr3t'}`) was not touched at all, and neither was a
  triple-quoted `'''…'''` — which `_format_toml_error` then underlined with a
  caret, because it believed it had redacted nothing. All three reached every
  destination that redacts: `--log-file`, the console's log buffer served to
  read-only viewers, and the config parse error rendered in a browser, which
  quotes the offending line. A quoted value now runs to its matching quote — a
  backslash-escaped one does not close it — and to the end of the line when the
  string is unterminated, which is the usual reason the parse failed on that
  line in the first place. Neither bound crosses a newline, so a value written
  across several lines is masked only as far as its first newline, and a `'''`
  or `"""` that ends the line leaves nothing on it to mask.
