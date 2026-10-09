- **MIDI Program Change did nothing in the `midi` scene, though it is on by
  default.** `midi_program_change` defaults to `True` and the scene's dispatch
  has handled Program Change all along — but the reader thread that feeds it
  only forwarded note on/off, so the message was dropped before reaching the
  dispatch and no keyboard or DAW could select a voice's waveform. The reader
  now forwards everything it does not deliberately coalesce, so the two cannot
  drift apart again: the only types it holds back are the continuous
  controllers (pitch bend and CC) it collapses to their newest value on
  purpose.
