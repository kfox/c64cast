- **REU-pump audio no longer echoes under bitmap REU-staged video
  (#544).** With `[audio].use_reu_pump` on an `mhires`/`hires` scene that
  uses `use_reu_staged`, the pump's write head used to overrun the audio
  reader every 10.5-12 s, which you heard as an echo or overlap. The
  `reu_pump_governor` (on by default) now covers that path too. A governed
  pump also runs 1.5x faster than matched and skips the surplus, because
  a pump that only matched the reader fell behind it under bitmap video.
  Measured on an Ultimate 64 with firmware 3.15a, the write head now stays
  at least 3.7 KB ahead of the reader. The REU mic pump is unchanged.
