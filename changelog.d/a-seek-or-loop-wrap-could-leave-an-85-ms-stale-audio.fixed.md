- **A seek or loop wrap could leave an ~85 ms stale-audio echo in the ring.**
  The audio worker holds one chunk in flight, and a transport splice that
  landed while it did dropped that chunk without writing anything into the ring
  span it had already been assigned — so the NMI replayed that span from one
  ring lap earlier, in the one place the splice design promises a
  constant-latency crosscut. The span is NEUTRAL-filled now, which also keeps
  the pacing servo's idea of the write head truthful across the splice. The
  pause path happened to cover this; a plain seek or loop wrap did not.
