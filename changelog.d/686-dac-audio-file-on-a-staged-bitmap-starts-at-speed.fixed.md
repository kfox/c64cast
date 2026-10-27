- **An audio file on the `$D418` DAC over a hires or mhires scene no longer
  starts about 3.5 % slow and flat on an Ultimate 64 running NTSC.** The REU
  stages those modes' frames, so the scene's writes over the link
  underestimated how many NMI ticks the player loses, and the track played slow
  and low for its first five seconds or so, until a measurement corrected it.
  It now starts at the speed measured on that machine and keeps trimming as it
  plays. An Ultimate 64 on PAL, an Ultimate II+ and a TeensyROM+ are not
  measured yet and start as before.
