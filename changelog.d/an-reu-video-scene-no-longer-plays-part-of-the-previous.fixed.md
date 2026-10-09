- **An REU video scene no longer plays part of the previous scene's soundtrack
  after a network blip during setup.** The track upload, its silent tail and
  the REU mic ring's silent prefill are now confirmed slice by slice and
  resent when lost. Before, a slice dropped by a broken DMA connection left
  the previous track's audio (or loud noise) in its place, with only a log
  warning. If a slice still does not land, the scene plays without audio.
