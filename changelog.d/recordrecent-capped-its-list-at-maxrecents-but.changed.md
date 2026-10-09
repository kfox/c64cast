- `record_recent` capped its list at `MAX_RECENTS`, but `set_favorite`
  (`console_library.py`) had no equivalent — a client holding the write
  token could loop distinct refs and grow `console.json` (read-modify-
  written whole, on every call, and served back to every browser and phone
  pointed at the host) without bound. Favorites are now capped at a new
  `MAX_FAVORITES`, and both `set_favorite` and `record_recent` reject a ref
  over 512 bytes. Separately, an empty ref used to be accepted, appended,
  and returned, only to be silently dropped by `_load`'s own filter on the
  very next read — both methods now treat a falsy ref as a no-op, so the
  return value never disagrees with what's actually persisted.
