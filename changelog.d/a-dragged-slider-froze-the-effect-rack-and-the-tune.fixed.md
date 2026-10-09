- **A dragged slider froze the effect rack and the tune panel for the rest of
  the session.** Both panels skip a rebuild while something inside them has
  focus (so a rebuild can't drag the handle out from under a finger), and a
  range keeps focus after a drag and a `<select>` after a change — so the first
  gesture stopped that panel updating until the performer happened to focus
  something else: a bypass flipped from a MIDI pad no longer showed, and after a
  scene advance the panel kept offering the previous scene's knobs. Both blur
  when the gesture ends, as the WLED page already did.
