- **A live mic on `[audio].use_reu_pump` keeps the host's share of its delay
  near 133 ms at 12 kHz (40–250 ms under mhires) instead of drifting.** Nothing tied the host's position in the REU mic ring to the
  pump that plays it. Under REU-staged `mhires` the delay grew by about
  1.8 seconds every ten seconds, until after about 34 s the host overwrote
  audio that had not played yet. Under `petscii` the pump caught up with
  the host after about 50 s and from then on played audio a lap (about 5 s)
  old. A host-side loop now reads the pump's position once a second and
  trims the input to hold the delay. A drift of up to 3 % is absorbed by
  resampling. A larger one, such as mhires's ~15 %, is absorbed by short
  crossfaded cuts that skip some input, so the pitch stays put. If the reads
  fail, the loop opens and the log says so (#560).
