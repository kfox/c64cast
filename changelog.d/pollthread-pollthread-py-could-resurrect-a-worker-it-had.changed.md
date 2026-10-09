- `PollThread` (`_pollthread.py`) could resurrect a worker it had just
  abandoned: `stop()` joined with a bounded `join_timeout` (0.5 s default)
  and then unconditionally cleared `self._thread` even when the join timed
  out and the target was still running (e.g. `RssOverlay`/`WeatherOverlay`'s
  `requests.get(timeout=5.0)` outliving it on a slow feed). A later `start()`
  then saw `is_running() == False`, called `self._stop.clear()` — which the
  still-running worker reads through the same shared `Event` — and spawned a
  second thread on top of the first, un-stopped. `stop()` now keeps the
  thread reference on a timed-out join (logging a warning) instead of
  discarding it, so `is_running()` stays truthful and `start()`'s existing
  "already running" no-op refuses the duplicate until the abandoned worker
  actually exits. Separately, an unhandled exception from a target used to
  end the thread via `threading.excepthook`, which prints straight to raw
  stderr and bypasses `--log-file`/`SessionLogBuffer` entirely — `_run` now
  catches it and calls `log.exception`, stopping the loop the same way as
  before but leaving a record in both durable sinks. `__init__`'s single
  `Callable` annotation also hid that periodic and manual mode want
  incompatible target signatures (`() -> None` vs. `(Event) -> None`); it
  now `@overload`s two constructor shapes so a wrong-arity target is a type
  error at the call site, with no change to any of the 21 consumer call
  sites. New `tests/test_pollthread.py` cases pin the abandon-then-refuse
  sequence and the exception-to-logging path.
