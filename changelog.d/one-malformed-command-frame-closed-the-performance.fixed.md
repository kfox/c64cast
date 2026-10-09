- **One malformed command frame closed the performance console's only feed.**
  `PerfBridge.apply` indexed `cmd["slot"]` / `["layer"]` / `["target"]` /
  `["index"]` directly and coerced with bare `int()` / `float()`, so a frame
  that decoded fine and then named an action without its fields — the shape a
  cached phone page from an older build sends — raised `KeyError` inside the
  WebSocket push loop, wrote a full traceback at default verbosity, and tore
  down the socket that carries state and log lines. That is exactly the outcome
  the frame decoder was written to prevent for an *undecodable* frame: the
  validation was enforced at the decode and defeated one layer down at the
  dispatch. Every field is now validated rather than coerced (including the
  bare `Infinity` / `NaN` literals `json.loads` accepts, whose `int()` raises
  `OverflowError`), a bad frame answers `{"ok": false}` instead of a 500 on
  `POST /perf/command`, and the socket loop guards the dispatch as well, so no
  raise from any engine a tap reaches can end the feed.
