- **A chip-count change mid-tune can no longer leave two writer threads racing
  one ring.** Re-initializing the player for a new SID count discarded its
  writer-thread handle after a bounded join, which is exactly the state the
  thread helper keeps a reference for: a writer still blocked in a DMA call was
  abandoned rather than waited for, and the restart then ran a second one
  against the same ring position counter. The handle is now kept, the loop exits
  on its own stop signal rather than a shared flag, and a start that would
  duplicate a live writer is refused and logged.
