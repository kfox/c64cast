- **Flicker blending and the host-DMA double buffer no longer show two frames
  at once on an Ultimate 64.** A frame could be written into the picture still
  on screen when it started before the C64 had flipped to the previous one,
  showing the frames before and after it together with a jagged seam (about 1
  to 2% of flicker frames). The C64 now reports each flip and the next frame
  waits for it. That costs 2 to 5% of the frame rate in hires and more in
  mhires: with 30 fps content, mhires drops to about 22 frames a second with
  flicker blending and 26 with the double buffer. The Ultimate II+ and
  the TeensyROM+ cannot report a flip and behave as before.
