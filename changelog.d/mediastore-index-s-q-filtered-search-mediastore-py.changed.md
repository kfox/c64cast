- `MediaStore.index`'s `q`-filtered search (`media_store.py`) applied its
  needle match *before* the `MAX_FILES` display cap so a search could reach
  media a plain listing had already truncated away — but that also meant a
  query matching nothing never tripped `truncated`, so it walked every
  configured root to `MAX_DEPTH` in full (resolving every kind-matching file
  along the way) with no way for the response to say the scan was unbounded;
  a search against a host rooted at `~` or an HVSC mirror could stall the
  console on one trivial `GET /api/media?q=` while it's also encoding video
  for a running show. `index` now also counts every candidate it visits
  against a new `_MAX_SCAN` ceiling (independent of `MAX_FILES`, an order of
  magnitude above it) and sets `truncated` once that trips.
