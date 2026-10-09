- **A REU-staged audio track longer than ~20 minutes overwrote the video
  staging region.** One byte is one sample, so the upload's footprint grows
  with the track's duration, and nothing at any layer bounded it: past
  `$E00000` at the 12032 Hz NTSC default it runs into the region the REU
  bank-swap bitmap path rewrites every frame, so the audio pump DMA'd bitmap
  bytes into the ring as full-scale garbage while the per-frame video writes
  shredded the audio — with no host-side error, on nothing more exotic than a
  long clip. The payload (and with it the EOF pad, which starts where the
  payload ends) is now bounded by the region and truncated with a warning.
