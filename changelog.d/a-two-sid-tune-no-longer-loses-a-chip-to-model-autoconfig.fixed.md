- **Model autoconfig no longer silences a SID chip by moving its socket out from
  under it.** When the first chip of a tune claimed the other socket (or
  UltiSID core) because it carried the right model, a later chip that had been
  playing on that socket was still counted as served there — and was left
  addressed to nothing. The later chip now gets a place of its own.
