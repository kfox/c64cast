- **With `[audio].use_reu_pump`, a video scene with `start_s` (or a URL
  timestamp) now plays the sound from `start_s`.** The soundtrack was staged
  from the start of the file while the picture began at `start_s`, so the
  sound ran `start_s` seconds behind the picture for the whole scene. The
  picture now also starts on the frame at `start_s` rather than on the
  keyframe before it.
