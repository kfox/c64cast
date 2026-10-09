- **With `loop_audio = "mute"` on bitmap video with `$D418` DAC audio, a
  seek now lands on the frame you asked for and the picture plays at normal
  speed afterward.** The transport clock ran in content seconds while the
  frames were stamped in the tempo-compensated domain, so a seek raced
  through (or held for) up to a fifth of the distance it jumped, and the
  picture then played about 14% fast, more once the scene had followed a
  slower drain.
