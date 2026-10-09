- **REU-pump audio under bitmap video no longer drops a few samples on
  every pass around the audio ring.** The pump moved 80 bytes at a time,
  which does not divide the 8 KB ring, so once per lap (about every 0.7 s
  at 12 kHz) the chunk that crossed the ring's end lost 48-79 samples. It
  now moves 64 bytes at a time, at the same byte rate.
