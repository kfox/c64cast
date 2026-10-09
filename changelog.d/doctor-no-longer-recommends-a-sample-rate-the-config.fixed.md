- **`--doctor` no longer recommends a sample rate the config stopped using.**
  The remediation hint on an unsafe `[audio].sample_rate` spelled its numbers
  out by hand and went stale: it still said "default 10500" two releases after
  12000 became the default, so the advice named a rate nobody was running. The
  default and both per-standard ceilings are now read from the config and the
  cycle-budget math, so the hint cannot contradict them again.
