- **Saving or clearing a loop slot no longer draws over the audience screen.**
  The transport engine's OSD line goes onto the C64's own output, so what
  belongs on it is transport *state* — `PAUSED`, `PLAY`, `SEEK 1:04`,
  `LOOP 1:04-1:31`, `REC ●`, and `LOOP 3` on a recall — all of which explain
  what the picture is visibly doing. `SAVED 3`, `3 CLEARED` and `NO LOOP`
  explained nothing on screen: they confirmed a control press and changed only
  a file on disk. They now go to the log, and the web console shows them
  properly — every state frame already carries the loop-slot list, so a slot
  filling or emptying is live feedback instead of a two-second flash. The MIDI
  and web surfaces stay identical, as they have been since Phase 2.
