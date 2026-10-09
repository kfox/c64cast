- `MediaStore.destination` (`media_store.py`) reported a kind whose
  configured directory doesn't exist yet with the same "not configured on
  this host" message as a kind nobody ever named for upload — sending an
  operator looking for a TOML setting that was already correct, since the
  host *is* configured and only the directory is missing. The two cases are
  now distinguished in the refusal message. Separately, `MediaRoot.writable`
  silently disagreeing with `_write_roots` — reachable only if a future
  refactor resolved read-only roots before write roots — is now an assertion
  in `resolve_root` rather than an invariant that depended on `__init__`'s
  two loops staying in this order with nothing to say so if they didn't.
