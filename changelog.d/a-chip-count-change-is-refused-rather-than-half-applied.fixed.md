- **A chip-count change is refused, rather than half-applied, while a writer is
  still blocked on the link.** Bring-up and re-init both assign the ring's slot
  size; doing that under a live writer sent the rest of its blocked burst out at
  the new stride, so slots landed across slot boundaries and the C64-side player
  decoded the op stream shifted — storing attacker-supplied bytes at
  attacker-supplied addresses anywhere in memory. Both now leave the player down
  instead, and say so; the next scene activation brings it up.
