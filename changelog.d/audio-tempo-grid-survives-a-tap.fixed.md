- **A tempo tap with `tempo_source = "audio"` no longer throws the beat grid
  billions of beats forward.** The audio drive stamped the grid with wall-clock
  time while taps and MIDI bytes use the monotonic clock, so the first frame
  after a tap measured the gap between the two clocks as elapsed time. Launch
  quantization, the console's beat readout and `mod_source = "clock"` effects
  now stay on the beat across a tap.
