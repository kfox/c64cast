- **Video scenes take `aspect_mode`, and a new `hybrid` mode joins it.** A
  video can now be letterboxed (`fit`) or stretched (`stretch`) instead of
  always cropped to fill, the same as a slideshow. `hybrid`, for both, crops a
  source halfway to the screen's shape and letterboxes the rest: a 16:9 film on
  a 4:3 screen shows as the broadcast 14:9, losing about 6% off each side
  instead of a quarter of its width. The default is still `crop`. The bars are
  added after the color statistics are read, so `auto_fit`, the per-shot fit
  and the forced and hardware palettes see only the picture.
