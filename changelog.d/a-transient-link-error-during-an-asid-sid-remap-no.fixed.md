- **A transient link error during an ASID SID remap no longer ends the scene.**
  The remap's hardware half is now guarded and retried on the next frame, and
  the active chip count is published only once the scope has the windows to
  match it. Previously a raise between the two left the scene indexing past its
  own window list on every later frame, which the playlist treats as a crashed
  scene and retires permanently.
