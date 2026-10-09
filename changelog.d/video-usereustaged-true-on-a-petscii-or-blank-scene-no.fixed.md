- **`[video].use_reu_staged = true` on a petscii or blank scene no longer
  corrupts the picture and the REU audio pump when `[audio].use_reu_pump` is
  on.** The screen push and the pump both drive the REU controller, and the
  pump's IRQ landed between the push's register writes on most frames and
  DMAd audio into screen RAM and on from there. Those scenes now
  push over host DMA while the pump is on, and log a warning when staging was
  asked for explicitly. Bitmap scenes keep REU staging alongside the pump.
