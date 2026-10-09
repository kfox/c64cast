- **A PERF button on the `/perf` console — performance mode.** While the C64 is in
  front of an audience, nothing should draw text over it, but a scrub, a knob
  sweep or a loop mark each post an OSD line. That readout is wanted while
  live-tuning at the desk, so it cannot be a static setting; PERF turns it off
  for the whole run and back on again. It silences every poster — live-tune,
  effect bypass, and the transport engine's `PAUSED` / `SEEK` / `LOOP A` /
  `REC ●` — and it survives a scene change. A double-tap of an `osd.position`
  pad now turns the same mode on, so the two are one control reachable from
  either surface. Turning it off restores whatever `[midi_control].osd` asked
  for rather than assuming "on", and posts nothing itself — a `PERF OFF` flash
  would be the confirmation-of-a-keypress this release took off that screen
  everywhere else. It ships on the `/perf` page; the Svelte console and the
  MIDI surface do not have the control yet, though `performance_mode` already
  rides every state frame for them to read.
