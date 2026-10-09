- **A video or audio-file scene on the Ultimate Audio sampler plays sound
  every time it comes round, not just the first time.** A scene keeps its
  sampler between plays, and stopping it latched the sampler shut. So when a
  playlist looped, or `--loop` repeated a single clip, every later play
  waited two seconds for audio that never came and then played silence.
  Audio-file scenes also stopped reacting to the music. The scene now resets
  the sampler before it starts feeding it.
