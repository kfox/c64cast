- **`[audio].sampler_clock_hz` outside 5000000..7500000 is refused when the
  config loads.** A slip such as `6160` or `61600000` used to be accepted
  and played sampler audio at the wrong speed, and `0` broke the sampler
  outright.
