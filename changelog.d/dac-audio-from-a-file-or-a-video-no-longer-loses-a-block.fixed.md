- **DAC audio from a file or a video no longer loses a block at startup.** A
  decoder running ahead of real time filled the `$D418` DAC's queue at once,
  and its next block waited behind the worker's first chunks for longer than
  the 200 ms put timeout, so about 93 ms of audio near the start was dropped.
  The wait now allows for the time the worker takes to drain room for the
  block.
