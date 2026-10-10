- **Bitmap scenes with a text overlay use REU staging again.** Under
  `[video].use_reu_staged = "auto"`, a `hires` or `mhires` scene carrying a
  text overlay (clock, marquee, callsign, …) now takes the REU bank-swap like
  any other bitmap scene, instead of the host-DMA page flip. Text stays crisp
  on it. `[video].double_buffer = "auto"` correspondingly turns on only on a
  backend with no REU (the TeensyROM).
