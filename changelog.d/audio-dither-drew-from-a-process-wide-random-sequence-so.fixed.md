- **`[audio].dither` drew from a process-wide random sequence, so a dithered
  capture could not be reproduced.** The realtime encoder fell back to numpy's
  global RNG, which every other numpy caller in the process shares and no run
  records — two takes of the same source produced different dither, and an A/B
  against `scripts/diags/quant_noise_ab.py` had nothing to hold fixed. Each
  `AudioStreamer` now owns one generator, seeded from system entropy and
  logged once at INFO (`audio: TPDF dither seed=N`) whenever dither is on, so
  a capture can be re-encoded from the seed its run wrote down. The dither
  itself is unchanged: same TPDF shape, same ±1 LSB, same exact-zero skip.
