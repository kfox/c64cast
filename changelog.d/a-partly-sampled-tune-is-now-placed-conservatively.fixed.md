- **A partly-sampled tune is now placed conservatively instead of optimistically.**
  Three places consult that trust flag; two of them logged a warning and then
  placed the player MC and the VIC display bank from the prefix anyway. The
  two whole-tune consumers (`waveform`, and a `generative` scene with
  `audio_source = "sid"`) no longer see the raw bitmap: on an untrusted sample
  both the player-avoid and display views widen to everything the tune was
  observed to touch, and the PLAY-time `$01` bank falls back to the address
  heuristic. A tune left with nothing free aborts its scene and the playlist
  advances, as it already did when no VIC bank was free. The SHIFT-cycle
  candidate walk now skips such a subtune the way it skips an unrenderable
  one, rather than repointing the display from a prefix mid-show — unless the
  display bank was pinned at startup, in which case nothing is being chosen
  from that sample and the subtune stays playable, cued only after any
  candidate whose own sample is whole.
