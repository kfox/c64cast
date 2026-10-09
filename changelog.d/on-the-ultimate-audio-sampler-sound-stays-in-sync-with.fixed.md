- **On the Ultimate Audio sampler, sound stays in sync with the picture after
  a seek, an A/B loop wrap, a resume from pause, or a decoding hiccup.** After
  every seek, loop wrap or resume, the sound had been running behind the
  picture by 0.15 s, and by about half a second once the clip took more than
  a moment to seek. A decoding stall long enough to run the audio buffer
  down added about 0.37 s more each time. The lag never recovered. Now, when
  the clip is catching up after a seek or a stall, the sound lines up with
  the picture again once it has. A clip catching up too slowly to line up
  within about two seconds keeps playing a little behind the picture until
  the next seek, loop wrap or resume. What a seek
  costs instead is the first few tens of milliseconds of the new position's
  audio. If the audio cannot catch up, it keeps playing behind the picture,
  as it did before, and a warning is logged. That happens with a source slower than real time, a live stream
  that resumes after a long stall, or a stream whose start timed out.
