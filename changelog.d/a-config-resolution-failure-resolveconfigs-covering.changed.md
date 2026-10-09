- A config-resolution failure (`_resolve_configs`, covering `load_master`,
  `merge_cli`, `quickcast.build_config` and `connect.parse_connection_uri`)
  logged only `str(e)` with no traceback, even under `-v` — a genuine
  internal defect anywhere in that tree was indistinguishable from a user
  typo and left oncall to bisect by hand. A `log.debug(..., exc_info=True)`
  now runs right before the existing `log.error`, so `-v` recovers the
  traceback; the exception types caught there are unchanged (still broad
  `ValueError`/`RuntimeError`, since legitimate config validation throughout
  `config.py` also raises plain `ValueError` and narrowing the catch would
  misclassify those as unhandled). The connection target resolved on the
  config-driven run path is now also logged at INFO with its source
  (`-u/--url` or `$C64CAST_URL`), so an env-var override can no longer
  silently repoint a run whose operator is reading a TOML that names a
  different host.
