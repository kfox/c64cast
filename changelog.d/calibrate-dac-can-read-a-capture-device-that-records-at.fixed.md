- **`--calibrate-dac` can read a capture device that records at 12 kHz or
  below.** Each slot's edges were trimmed by a fixed 24 samples, which at
  those rates left nothing to measure, so a clean recording was refused as
  holding no ring pass. The trim is now a settling time (0.5 ms) that scales
  with the capture rate.
