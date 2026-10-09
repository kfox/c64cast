- **A parse error in a config file copied the offending line's secret into
  the log.** `_format_toml_error` quotes the source line the TOML parser
  choked on, cli.py logs the resulting `ConfigError` at error level, and
  `--log-file` mirrors it to disk — so a syntax error anywhere on a
  `dma_password = "…"` or `token = "…"` line wrote the credential to a file
  that outlives the run, in the one situation where the log gets pasted into
  an issue. Such a line is now redacted (the position and the parser's
  message carry the diagnostic value; the value does not).
