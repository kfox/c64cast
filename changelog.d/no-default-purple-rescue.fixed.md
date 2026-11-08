- **Dark scenes and faces in shadow no longer turn purple.** Color shaping
  used to snap every violet-to-pink pixel to magenta and nearly double its
  brightness before matching it to the 16 colors. On a real VIC-II, a VIC-II
  Kawari and the Ultimate 64 alike, that sent lilac-lit shadows and shadowed
  skin to light blue and purple instead of dark blue, brown and black. The band is no longer applied
  by default. To keep the old look, define it as a `[[color.hue_corrections]]`
  band: the example config shows it, commented out.
  `hue_corrections_replace_defaults` now has no effect, since there are no
  built-in bands to replace.
