- **Bright areas of mhires video no longer stick on a near color on a real
  C64.** With the `c64-wiki` palette (called `pepto` before this release, and
  what `auto` chose for a real VIC-II, such as a TeensyROM+ host),
  `motion_smoothing` held pixels and bitmap codes on cyan as a white face
  brightened, until most of the face was cyan for the rest of the shot. A held
  color now gives way whenever the input moves well past it toward another
  palette color, so palettes whose entries sit close together release as
  quickly as the Ultimate 64's. Hires cells get the same release, which also
  frees a cell held on a solid color next to a close `flicker_tolerance` blend.
