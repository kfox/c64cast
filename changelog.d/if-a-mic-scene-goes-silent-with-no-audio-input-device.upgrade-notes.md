- **If a mic scene goes silent with "no audio input device matched", fix
  `[audio].device` or `--audio-device`.** A device name that matches no
  input, or an index that is not an input, used to fall back to the system
  default input without saying much. On a laptop that is the built-in
  microphone, which then played the room through the C64. Now a webcam or
  blank scene logs an error and plays without sound, and a scene with
  `audio_source = "mic"` or `"listen"` logs it and is skipped. `-1` still asks
  for the default input.
