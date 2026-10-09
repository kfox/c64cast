- `--save-settings` could raise `ConnectionURIError` (a `ValueError`) straight
  out of `cli.main()` as an uncaught traceback on a bad `-u` target, instead
  of the exit-2 usage error `connect.py`'s own docstring promises — it is
  dispatched before `_resolve_configs`' try/except, and had no guard of its
  own. All of `main()`'s config-free terminal commands (`--save-settings`,
  `--install-char-rom`, `--check-for-updates`, `--upgrade`, `--motd-line`,
  `--reset-setup`) are now dispatched through one table wrapped in the same
  `ValueError`/`RuntimeError` → exit 2 mapping `_resolve_configs` already
  had, so a new command can't forget it. Separately, if an existing
  `settings.toml` already carries a hand-written `[ultimate64].dma_password`,
  `--save-settings` can never re-write it (secrets are suppressed on save) —
  which used to mean the very next `--save-settings` silently dropped it on
  the merge; it now warns at save time instead. `--save-settings --help`
  also stopped listing `-D/--audio-device`, which it has always persisted;
  the whitelist that drives the help text, the "nothing to save" error, and
  the apply block is now one table (`cli_commands.SAVABLE_SETTINGS_FIELDS`)
  instead of three hand-copied lists that could (and did) drift.
