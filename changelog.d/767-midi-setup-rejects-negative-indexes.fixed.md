- **`--midi-setup` no longer takes a negative number as an index.** Removing
  mapping `-1` in the review used to delete the last mapping, and a scene-jump
  pad learned with scene `-1` was saved as a pad that does nothing. Both now
  report the bad index and change nothing.
