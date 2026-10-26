- **A palette or color pick from the WLED app no longer races the frame being
  drawn.** It used to reset the display mode's palette state and drop the
  screen's dirty cache from the WLED server's thread; it now takes effect at the
  start of the next frame, and a pick made just before a scene change is dropped
  rather than applied to the next scene.
