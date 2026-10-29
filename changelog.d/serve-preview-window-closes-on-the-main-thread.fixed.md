- **Stopping a `--serve` show with `[preview]` on no longer crashes the host.**
  The show's teardown closed the preview window from a background thread,
  which macOS answers by killing the process before the machine is reset. The
  window is now closed only by the main thread that draws it.
