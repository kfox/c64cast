- **A capture block that arrived as a flat array crashed the mic callback.**
  All three capture paths spelled their mono fallback in a way that could only
  raise `IndexError` on the one input it existed for — inside a PortAudio
  callback, where the traceback goes to stderr rather than the log and audio
  simply stops. One shared downmix now, correct on both shapes. In the same
  callback, a link failure during a REU mic write is caught, counted and logged
  once per run instead of killing mic audio for the rest of the scene with
  nothing in the log to point at.
