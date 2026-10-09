- **The documented `duration_s` default was wrong for a quick-playback audio
  file.** The config metadata — which the JSON schema, `--describe` and the
  reference guide's scene-type appendix all render from one string — said
  "everything else = 30s", but a `generative` scene with
  `audio_source = "file"` and no explicit `duration_s` is sized to the decoded
  track, and that is exactly the scene `c64cast tune.mp3` builds. Anyone
  reading the schema to find out why a song kept playing past 30 seconds was
  told the opposite of what the code does. The help and the guide's vocabulary
  chapter now name that case, and say the 30 s fallback applies when the
  container reports no duration.
