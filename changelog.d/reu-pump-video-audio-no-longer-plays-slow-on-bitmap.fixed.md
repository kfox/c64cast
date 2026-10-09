- **Video audio through the REU pump (`[audio].use_reu_pump`) no longer plays
  slow and flat on `mhires` and `hires` scenes.** The bank-swap that brings up
  each new bitmap frame copied it in pieces too long for the NMI period at the
  12 kHz default (and in one piece on `hires`), so the C64 skipped audio
  samples during every frame, and the pump that refills the audio ring could
  not catch up on the ticks the bank-swap held it off for. On `mhires` the
  sound ran at 0.83 of real time; it now runs at about 0.98.
