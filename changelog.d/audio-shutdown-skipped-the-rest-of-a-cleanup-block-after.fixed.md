- **Audio shutdown skipped the rest of a cleanup block after one step failed.**
  Three stretches of teardown each sat under a single swallow, so one transient
  link error cost every write behind it in its own block: the KERNAL NMI vector
  — left pointing into RAM that the next scene is free to overwrite — together
  with SID volume and the DAC bias; or CIA #1's timer latch, leaving the jiffy
  clock, keyboard scan and cursor blink at the REU pump's kHz rate; or a
  microphone `close()` after a raising `stop()`. Each write is now guarded on
  its own and reports its own failure. The SID mute also moved behind the
  NMI-vector restore, which is what keeps a still-live NMI source from
  overwriting it.
