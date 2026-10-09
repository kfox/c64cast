- **The console's state feed did blocking disk and DMA work on the event loop.**
  Both `async def` socket routes called the frame builder and the command
  dispatcher directly: one frame reads the look store and the loop-preset store
  off disk (~3 times a second per connected console), and a border or background
  pick is a DMA write over TCP port 64 that is unboundedly long on a stalled
  link. That loop also serves `/status`, every `/api` route and the MJPEG screen
  stream, so a slow data directory or a stalled machine stalled all of them —
  while the sibling sync route got the threadpool for free and was never
  affected. Both now run off the loop.
