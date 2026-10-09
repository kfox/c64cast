- **The appliance setup form erased every secret in `settings.toml`.**
  `POST /api/setup` — unauthenticated while the setup window is open —
  seeded a `Config` from the machine-settings file (secrets included),
  overlaid the connection target, and rewrote the same file through
  `config_serialize.dumps`, which suppresses every secret field. So the
  first successful setup silently dropped `[ultimate64].dma_password`
  (leaving an appliance unable to talk to its own password-protected U64)
  and `[web].token`/`token_file`/`viewer_token` — including a `[web].token`
  pin, which is the one thing `token_settable` exists to protect: the form
  correctly refused to *replace* a pinned token and then deleted it anyway,
  so the next restart minted a brand-new credential and the URL the admin
  had been handed was dead. Both writers of that file now go through one
  `config_serialize.save_machine_settings`, which preserves what the merge
  read; `--save-settings` stops warning that it is about to drop a
  hand-written `dma_password` because it no longer does, prints the
  secret-free rendering of what it saved (naming the preserved keys, never
  quoting them), and the file is restricted to `0600` when it carries one.
  `setup_api._write_connection`'s docstring used to *assert* it mirrored
  the CLI's save path "exactly" while missing the guard that path had.
