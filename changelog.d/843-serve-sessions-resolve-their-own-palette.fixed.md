- **Each `serve` session uses its own machine's palette.** A later session
  kept the first session's host palette and logged an ensemble mismatch
  warning, so colors were matched against the wrong 16 for the rest of the
  daemon's life. The palette is now settled afresh when a session starts.
