- `--dump-char-rom`'s teardown swallowed a reset failure entirely
  (`contextlib.suppress(Exception)`) even though the reset exists so the
  machine "isn't left parked wherever the dump stub ran" — the one outcome
  worth knowing was exactly what got hidden, at every verbosity, while the
  success message still printed. It now logs a warning naming the failure
  instead. `be.close()` in the same `finally` was unprotected, so a close
  failure on the unresponsive-machine path (the case most likely to hit one)
  replaced the deliberate `return 3`/`return 4` with a traceback; it is now
  guarded the same way.
