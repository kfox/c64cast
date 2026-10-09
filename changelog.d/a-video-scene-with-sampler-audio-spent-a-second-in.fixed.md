- **A video scene with sampler audio spent a second in teardown.** Closing the
  video source bounded-joins the demux thread that feeds the sink, and on the
  sampler that thread parks in `push_samples` until the sampler stops — which
  is what the audio stop does, and it ran behind the close. The close burned
  its full 1 s bound and logged a join timeout; the audio now stops in front
  of it.
