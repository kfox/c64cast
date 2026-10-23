- **A TeensyROM+ whose firmware can lock its DMA writes to the `$D418` sample
  player plays DAC audio without losing a sample while it streams video.** Each
  write goes out in slices that start right after the player stores a sample
  and end before its next one, so no write ever holds the 6510 across a
  sample. In a blind listen to a music video, the locked audio was picked over
  unsliced audio, which still wobbles audibly. Bitmap video with DAC audio now
  plays in real time, and short-lived frames are kept as well as before. This
  is `[teensyrom].dma_slicing = "auto"`, the new default; on firmware without
  the lock, writes stay unsliced as before (`"locked"` warns then). `"on"`
  slices without the lock at `dma_slice_bytes` / `dma_slice_gap_us` (TR+ v0.9
  or later): it loses fewer samples than unsliced writes, but drops some
  short-lived bitmap frames. Writes stay unsliced when no sample player is
  running, and a regular TeensyROM never slices.
