- **The sampler and the ASID player no longer start over stale REU audio
  after a lossy reconnect.** Their ring prefills (and the sampler's first
  prebuffer write) were sent once and not checked, so a slice lost on the link
  left the previous scene's audio in the ring: the sampler played it until the
  writer caught up, and an ASID ring left at another chip count's slot size
  could misalign the player. They are now confirmed and resent like the REU
  pump's install; an ASID prefill that never lands keeps the buffered player
  off for that activation.
