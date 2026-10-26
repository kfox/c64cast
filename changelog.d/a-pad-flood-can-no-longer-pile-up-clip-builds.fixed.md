- **A burst of clip-pad presses no longer starts a build thread per press.** Presses
  that arrive within one frame now start one build, at most four builds run at
  once while a slow clip (a URL that takes a while to resolve) is still loading,
  and the pad and look queues hold the newest 64 events while the show is
  paused instead of growing without limit and replaying all of them on resume.
