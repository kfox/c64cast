- **A `0x31` speed flood can no longer freeze video and starve audio.** The only
  throttle on the ASID retune was dropping a request identical to the one in
  force, which alternating any two rates defeated — and 999 Hz and 1000 Hz are
  both legal, so nothing even warned. Each surviving message cost a blocking
  write plus a round trip on the single DMA link the video path shares, from the
  MIDI reader thread, so an ordinary 60 Hz arrival rate spent most of the link
  budget and let the MIDI input queue grow without bound behind it. Retunes are
  now rate-limited to one per 250 ms; a request inside that window is coalesced
  rather than dropped, so the newest speed still takes effect.
