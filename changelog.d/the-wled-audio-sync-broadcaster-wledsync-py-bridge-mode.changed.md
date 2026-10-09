- The WLED audio-sync broadcaster (`wled_sync.py`, bridge Mode 3) could raise
  an unhandled `AttributeError` out of its emit thread if `stop()` ran between
  a tick's null-check and its `sendto` call; `_emit` now binds the socket to a
  local first. Its running failed-send count is now readable (`send_errors`)
  and `stop()` logs it as a one-line summary when nonzero.
