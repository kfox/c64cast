- An all-generative playlist with `audio_source = "sid"` never got the
  ensemble audio-contention warning. `_scene_contends_for_audio` claims to
  mirror `Scene.competes_for_audio_lock()` and omitted `generative`, whose
  SID arm builds a source with `wants_audio_lock = True` — so the exact
  footgun that warning exists for shipped silently: the system idled whenever
  another held the slot and the user was told nothing. The mirror is pinned by
  a test now.
