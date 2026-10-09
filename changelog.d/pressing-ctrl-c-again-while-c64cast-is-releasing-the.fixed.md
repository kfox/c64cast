- **Pressing Ctrl+C again while c64cast is releasing the machine after a
  failed start now finishes the job instead of abandoning it.** The release
  steps that were left (the reset, closing the connection, putting back the
  REU, sampler, volume, video output and palette settings) still run, and
  c64cast exits once they are done. Press Ctrl+C once more to stop at once
  and skip what is left.
