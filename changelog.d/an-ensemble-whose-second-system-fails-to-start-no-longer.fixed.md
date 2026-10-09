- **An ensemble whose second system fails to start no longer leaves the first
  one held.** When a later system's setup failed with anything but c64cast's
  own startup errors (an unexpected network error, say, or Ctrl+C during
  startup), the systems already started kept their connection open and their
  REU, sampler, master volume, video output and palette changes in place.
