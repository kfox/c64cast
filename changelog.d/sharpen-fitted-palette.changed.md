- **mhires video isn't sharpened on a palette fitted to the source.** With
  `hardware_palette = "source"` or `"auto"`, `[color].sharpen = "auto"` now
  turns sharpening off while the fitted palette is showing, so thin bright
  lines such as a film's grid stay whole instead of breaking into dashes.
  Against the machine's own palette, video is still sharpened at 0.3. A
  number set for `sharpen` applies on both.
