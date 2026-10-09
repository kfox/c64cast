- **A write the link lost no longer goes unnoticed because another part of
  the show flushed first.** The audio and render threads share one DMA
  connection, and a flush on one used to clear the "commands may be lost"
  report for both, so a SID or program launch could fire on top of a write
  that never landed. A lost write is now charged to the thread that sent it,
  and a launch refuses only for its own thread's losses.
