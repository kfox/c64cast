- **`--serve` reported success for a run that never served.** uvicorn binds
  on its background thread, not in `ControlServer.start()`, and when the port
  is already in use — a second `--serve` on the same host, the likeliest
  operator error — it calls `sys.exit(1)` *there*: a `SystemExit` that the
  poll thread does not catch and that Python's thread hook discards without a
  record. So the host logged "listening on http://…", printed a login URL,
  autostarted a show on real hardware, parked forever with nothing listening,
  and exited `0` on the eventual Ctrl+C. `start()` now waits for uvicorn to
  confirm the bind before it claims to be listening and answers whether it
  is; `--serve` exits `2` with the reason named, and never advertises,
  banners or autostarts a console that isn't there. (This also reaches the
  WLED device server and the `[control]` plane, which get the error line
  instead of a false claim.)
