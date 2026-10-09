- **With `[audio].use_reu_pump`, a stray audio packet stamped far ahead of
  its picture no longer silences the rest of the clip.** The staged sound
  was preceded by silence up to its stamp, to the size of the REU; it now
  follows on from the audio before it, as the other audio paths do.
