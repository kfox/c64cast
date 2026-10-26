- **A border, background or palette pick from a MIDI controller or the web
  console no longer races the frame being drawn.** A live-tune choice used to
  write `$D020`/`$D021` or drop the screen's dirty cache from the thread that
  received the move; it now takes effect at the start of the next frame. A pick
  made just before a scene change is dropped rather than applied to the next
  scene.
