- **The framebuffer kept shadowing every DMA write for a disabled feature.**
  With `[preview]` off and `[recording]` on, a recorder that refused to start
  (a codec/fourcc the platform will not open) left the shadow-memory write
  listener registered for the rest of the run with nothing reading it. It is
  now detached when no consumer survives, and at teardown.
