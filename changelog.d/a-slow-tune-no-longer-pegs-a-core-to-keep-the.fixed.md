- **A slow tune no longer pegs a core to keep the oscilloscope in step.** The
  poll thread catches the host emulator up to wall clock each wakeup, bounded
  by a tick count that assumed each tick costs 0.2 ms. A PLAY that stays
  legally inside the emulator's per-pass budget can cost 15.8 ms, making the
  same batch 1.9 seconds long on a thread whose period is a sixtieth of a
  second — and the tune sets the rate the batch is sized against. The batch
  now also stops after half a poll period, leaving the rest to the renderer,
  and says once that the scope is running behind the audio.
