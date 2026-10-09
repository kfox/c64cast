- `silence_native_stderr` (`_native_io.py`) had two independent descriptor
  leaks on its own failure paths (`saved = os.dup(2)` sat outside its `try`,
  so a failing `os.open(os.devnull, ...)` leaked it; `os.close(devnull)` sat
  after `os.dup2(devnull, 2)` inside the `try`, so a failing `dup2` leaked
  `devnull`), and no mutex or nesting depth around its dup/dup2/close
  sequence on the process-global fd 2 — two overlapping (non-nested) callers
  (reachable in practice: `video._ensure_pyav` is a lazily-triggered entrant
  from playlist worker threads, one per system in an ensemble) left the
  second caller's `os.dup(2)` capturing the first caller's `/dev/null`
  redirect as its own "saved" fd, so whichever exited last pinned the
  process's real stderr to `/dev/null` permanently. A module-level depth
  counter behind a lock now makes the redirect reentrant across both nesting
  and overlap (only the outermost enter/exit touches fd 2), and both
  descriptors are released on every failure path. New `tests/test_native_io.py`
  — previously nothing imported this module at all — pins silencing,
  restoration, the overlapping-threads case, and a 200-cycle no-fd-growth
  check; it skips on Windows, where `os.set_blocking` (the fixture's way of
  draining fd 2's pipe without blocking) doesn't exist.
