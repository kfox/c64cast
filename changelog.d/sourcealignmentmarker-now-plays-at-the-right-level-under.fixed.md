- **`source_alignment_marker` now plays at the right level under a Mahoney DAC
  curve, and `find_marker_in_capture` finds the marker at the effective rate.**
  The chirp was always written as 0-15 volume codes, so with `dac_curve` on it
  played without the filter bits the track's bytes carry; it now goes through
  the active curve. The capture reference held each code for a whole number of
  capture samples, so at the NTSC 12032 Hz rate against a 48 kHz capture it was
  a quarter short and the anchor came out about 18 ms late; it now uses the
  true ratio.
