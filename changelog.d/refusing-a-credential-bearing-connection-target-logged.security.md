- **Refusing a credential-bearing connection target logged the
  credential.** `connect._reject_userinfo` refuses a `user:pass@host` target
  precisely so a secret cannot reach `[ultimate64].url`, from which
  `--save-settings` writes it to `settings.toml` and echoes it to stdout —
  and then interpolated the whole target, credential included, into the
  error. Every parse failure now reports the target through
  `connect.redact_target`, which masks userinfo and secret-shaped query
  values while keeping the host, and `[ultimate64].url`'s own messages and
  its debug line use the same spelling.
