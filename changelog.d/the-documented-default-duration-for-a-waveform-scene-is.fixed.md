- **The documented default duration for a waveform scene is now the one the
  code uses: three minutes, not thirty seconds.** A tune with no explicit
  `duration_s` and no song-length database match has run for 180 seconds for a
  long time; the reference guide, the `duration_s` help text, the generated
  scene-type appendix and the JSON schema all said 30, a six-fold error that
  reads as a playback bug when a jukebox holds each tune for three minutes. The
  same help text credited `midi` scenes with song-length resolution they have
  never had. Documented alongside it: a waveform scene can also end *early*,
  six seconds after every voice falls silent, which it has done since the
  silence detector shipped.
