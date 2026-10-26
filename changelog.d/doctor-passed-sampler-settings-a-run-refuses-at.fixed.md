- **`--doctor` passed `[audio]` sampler settings that a run refuses at
  startup.** An unsupported `sampler_bits` or a `sampler_clock_hz` written in
  kHz passed `--doctor --skip-probe` and then stopped every run with exit 5.
  `--doctor` now reports them.
