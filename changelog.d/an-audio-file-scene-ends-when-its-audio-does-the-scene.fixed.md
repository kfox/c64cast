- **An audio-file scene ends when its audio does.** The scene used to last
  as long as the file's header said, so a truncated download, a file that
  stopped decoding, or a header claiming a wrong length played silence for
  the difference. One test file claimed almost five years. With a folder or
  glob of tracks, the scene also used to take the length of the first track
  picked rather than the one playing. An explicit `duration_s` or `-t` still
  cuts the scene short.
