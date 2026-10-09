- **A short chunk during the audio prebuffer could push a ring write past the
  end of the ring.** The worker's NEUTRAL tail pad was gated on the consuming
  phase, so a collect window that closed short while prebuffering was written
  at its raw length and took the ring write pointer off the chunk grid the ring
  size is an exact multiple of. Both wrap guards check the address only after
  the increment, so the next chunk to cross the boundary went out first and its
  tail landed outside the ring — never played, and over memory another scene
  uses. Reachable on the ordinary mic path, where the worker starts before the
  input stream opens. Every short chunk is padded now; the partial-underrun
  counter stays consumption-phase-only.
