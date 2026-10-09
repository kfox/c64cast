- **A dead audio worker was invisible, and could come back as a second one.**
  `stop()` joins the worker with a one-second bound, because a ring write on a
  stalled link can outlast it — but it neither said so nor stopped the next
  scene from starting a second worker into the same ring. A survivor is now
  reported, and each worker is fenced to the start that created it, so it exits
  on its own instead of being resurrected by the next scene's start. Worker
  liveness also joins `AudioStreamer.stats()`, which is what the crash
  handler's flag was always for.
