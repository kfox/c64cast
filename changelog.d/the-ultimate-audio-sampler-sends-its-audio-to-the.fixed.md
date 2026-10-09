- **The Ultimate Audio sampler sends its audio to the Ultimate in about 40
  writes a second, whatever the source's frame size.** It had sent one write per decoded frame, so audio decoded in
  2.5 ms Opus frames took about 400 writes a second, twice what the link
  can carry. Even after that was fixed, it fell back to one write per frame
  for a moment after every seek, loop wrap or resume, and while a source
  slightly slower than real time, such as a live stream, ran its audio
  buffer low. In the
  other direction, a very large decoded block, such as a low-rate FLAC
  block, was written whole: past the audio buffer and around the ring,
  holding up a seek until it was done. And a write the link failed to
  deliver lost its audio; it is now retried.
