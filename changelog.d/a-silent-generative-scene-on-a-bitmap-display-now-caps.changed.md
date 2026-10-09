- **A silent generative scene on a bitmap display now caps at half the system
  rate (30 NTSC / 25 PAL), like every other frame-pushing scene.**
  `audio_source = "none"` and `"listen"` were the only always-fresh bitmap
  scenes with no frame cap at all, pushing a full ~9-10 KB frame every tick at
  60/50. Both skipped the cap on the reasoning that they drive no DAC — but the
  muted bitmap cap was never about audio; it is host-DMA tear, which a silent
  generator causes exactly as much of as a loud one, and which is why the WLED
  pixel sink (no audio whatsoever) and SID-sourced generative scenes have
  always capped. Char displays are unaffected, and an explicit `target_fps`
  still wins. Animation on those scenes is half as smooth and should tear
  visibly less.
