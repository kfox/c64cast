- **Closing the console mid-rewind or mid-fast-forward no longer leaves the
  video scrubbing.** A held ◀◀/▶▶ (or `[`/`]`) sent its release only when the
  button came up, so a tab closed or a connection lost mid-hold ramped the
  video to 30x until the scene ended. The console now repeats the press while
  it is held, and the show lets go within 2 seconds of the last one. MIDI
  holds are unchanged.
