- **A TOML syntax error on a `dma_password` line could echo the password into
  the log, the console and the browser.** The parse error quotes the offending
  source line, and it was checked by asking whether redacting had *changed* the
  text — which a malformed line satisfies without the value being touched.
  `dma_password == "hunter2"` had its doubled `=` masked and the passphrase left
  whole, and because something had changed the caret was suppressed, so the one
  signal that the line was protected fired over an intact credential. Near
  misses redacted nothing and kept the caret pointing straight at the value
  (`dma_password "hunter2"`), and a parse failure *inside* a `"""`-quoted
  password echoed the passphrase itself, since a continuation line carries no
  key name to match. All of it reached `--log-file`, the log buffer the console
  serves to read-only viewers, and the parse error the console renders in a
  browser. The quoted line is now decided by where the secret-shaped key *is*
  rather than by whether a substitution happened: such a line keeps the key name
  and loses everything after it, and a line inside a value that is still open —
  a multi-line string, an array, an inline table — is dropped whole. A line
  carrying URL userinfo is truncated at the scheme, which is the one place a
  credential sits under a name that is not secret-shaped
  (`url = "u64://user:pass@host"` names `url`) — and that cut wins when it
  comes first, since the key name can sit past the password
  (`url = "https://user:pass@host/feed?api_key=x"`). Whitespace does not end
  that netloc, so a passphrase with a space in it no longer rides out whole.
  And the line the parser pointed at is now found by counting `\n` the way the
  parser counts it, rather than with `splitlines()`, which also breaks on
  `U+0085`, `U+2028` and `U+2029`: one of those in a value above the failure
  shifted every line index after it, and what got quoted was a fragment of the
  passphrase carrying no key name — echoed verbatim, caret and all. A line with
  no secret on it is still echoed in full with its caret.
