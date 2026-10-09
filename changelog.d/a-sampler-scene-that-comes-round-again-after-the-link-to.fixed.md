- **A sampler scene that comes round again after the link to the Ultimate
  stalled mid-write no longer plays scrambled audio.** The stalled write could
  outlast the scene's stop, and the next play then ran a second writer beside
  it, so the two fed the same ring out of order. While the stalled write is
  still stuck, a video scene now plays that lap silent and an audio-file scene
  is skipped.
