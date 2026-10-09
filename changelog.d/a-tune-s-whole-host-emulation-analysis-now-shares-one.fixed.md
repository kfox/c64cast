- **A tune's whole host-emulation analysis now shares one time budget, and a
  cut-short measurement says so.** Scene setup emulates a SID once per
  footprint and once per subtune — up to 18 runs — and each run drew its own
  wall-clock deadline, so a 306-byte file declaring 16 subtunes blocked the
  main thread for 43 seconds before the first note, and one SHIFT press
  re-spent it with the audio already silenced. The runs now share a single
  six-second budget, an INIT is bounded by the clock instead of only by an
  emulated-cycle count, and a run that gives up early is reported as an
  incomplete sample rather than returned as if it had finished. An incomplete
  sample no longer pins one display bank for every subtune, and it is called
  out in the log where the C64-side player's RAM slot is chosen from it.
