- **Cycling a blended hires scene to its edges styles no longer stops the
  scene.** With `[color].flicker_tolerance` on, the style key moved the picture
  to `edges` and the next frame failed to upload; the edges styles now play
  with both pages holding the same picture.
