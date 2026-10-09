- **`--profile`'s p50 was one sample too high.** The percentile index
  truncated where nearest rank rounds up, so at the steady-state 64-sample
  window the reported median was the 33rd smallest frame time rather than the
  32nd (and the "median" of two samples was the larger one). p95 was affected
  at some window sizes too.
