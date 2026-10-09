- **An audio-file scene's teardown paused for its whole join timeout, then
  dropped the decoder it had failed to join.** A decode thread parked in
  `push_samples` on a full sampler queue is released by the audio stop, which
  ran *behind* the join — so the join spent its full 2 s and returned with the
  thread still running, and the reference was cleared anyway. Teardown now stops
  the sink ahead of the join, and a decoder that still survives it stays
  referenced: the next `setup()` on that source refuses to clear its stop signal
  or start a second decoder until the survivor exits.
