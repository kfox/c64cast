- `[web].viewer_token` set to the same value as `[web].token` is now refused
  when the credentials are resolved, rather than at app construction:
  `auth.match_role` compares the full token first, so one secret pasted into
  both fields — or both fed from a single secret-manager entry — silently
  granted every holder of the "read-only" link start, stop, config writes
  and media upload. The refusal names the reason and exits `2` instead of
  raising a `ValueError` out of the middle of a FastAPI app build.
