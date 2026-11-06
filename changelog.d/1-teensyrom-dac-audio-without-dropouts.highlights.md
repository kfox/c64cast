- **On a TeensyROM+, DAC audio no longer drops samples while video streams,
  and bitmap video keeps its full frame rate.** On firmware that can time its
  memory writes around the sample player, c64cast now does that by default, so
  no write lands on top of a sample. In a blind listening test, people
  preferred this audio. A MultiHires video that used to sag to about 14 fps
  holds 20 fps.
