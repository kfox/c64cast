- **A `display = "random"` slideshow's overlays were validated against one
  random pick.** `mcm` is in the pool and rejects a text overlay, so the same
  unchanged config loaded on roughly four runs in five and `--doctor` returned
  a different verdict from one invocation to the next — and when it did load,
  the runtime re-pick could still land on the rejected mode with no check at
  all. Every mode the pool can produce is checked now.
