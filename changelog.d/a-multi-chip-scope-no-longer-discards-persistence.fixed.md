- **A multi-chip scope no longer discards `persistence` / `scroll_columns` in
  silence — and gives them back.** Per-window trails are not supported, so a
  stream or tune revealing a second SID forces every voice to the plain redraw
  path; that happened with nothing in the log and nothing in the docs, so a
  configured `persistence = "long"` simply stopped trailing mid-scene. It now
  warns, and the ASID example lists the limitation. The force was also one-way:
  a waveform scene that played a 2SID tune and then a 1SID one never got its
  trails back for the rest of the run. The render modes are re-derived on every
  reflow instead of being overwritten.
