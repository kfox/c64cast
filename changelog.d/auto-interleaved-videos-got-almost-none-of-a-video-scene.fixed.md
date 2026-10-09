- **Auto-interleaved videos got almost none of a video scene's wiring.**
  `[playlist].interleave_videos` constructed its `VideoScene` directly and
  hand-copied one of the six things the video builder does — the frame-push
  cap. Everything else was silently absent, kept from crashing by the scene
  defaults: no `tempo_scale`, so the bitmap+`$D418`-DAC tempo compensation was
  switched off for exactly the hires_edges-over-DAC case it exists to correct
  and every interleaved clip played the documented ~11-12% slow while
  configured video scenes did not; no sampler resolution, so a sampler-capable
  Ultimate 64 played interleaved clips on the lo-fi 4-bit DAC (and took its
  20 fps cap) while every configured video scene in the same run got the
  off-bus sampler at full rate; and no `[color]` section, no
  `[midi_control].loop_audio`, no effect chain, no overlays, and none of the
  epilogue stamps — so `[midi_control].osd = "off"`, `[dsp].pre_emphasis` and
  `[debug].frame_numbers` were ignored on these scenes alone. They are built
  through `build_scene` on a synthetic video scene now, so there is nothing
  left to keep in sync by hand.
