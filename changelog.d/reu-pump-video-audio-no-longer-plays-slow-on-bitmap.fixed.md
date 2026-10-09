- **Video audio through the REU pump (`[audio].use_reu_pump`) no longer plays
  slow and flat on `mhires` and `hires` scenes.** The bank-swap that brings up
  each new bitmap frame copied it in pieces too long for the NMI period at the
  12 kHz default (and in one piece on `hires`), so the C64 skipped audio
  samples during every frame; on `mhires` the sound ran about 17 % slow
  against the picture. The pieces are now short enough for every sample rate the audio
  path accepts.
