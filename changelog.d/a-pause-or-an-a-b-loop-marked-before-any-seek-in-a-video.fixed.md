- **A pause, or an A/B loop marked before any seek, in a video with a
  `start_s` keeps its place in the file.** The resume jumped back by
  `start_s`, and loop A was marked that far short, so the loop wrapped to the
  wrong place.
