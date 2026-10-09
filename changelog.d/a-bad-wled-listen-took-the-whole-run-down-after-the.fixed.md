- **A bad `[wled].listen` took the whole run down after the hardware was
  already up.** The endpoint was parsed at service-start time, outside the
  guard that is supposed to keep one optional surface from killing a session,
  so `listen = ":70000"` produced a traceback and an unmapped exit code with
  every machine already open, reset and provisioned. It is now rejected by
  `--doctor`-grade config validation *before* any hardware is touched (exit
  `5`, like every other config error), and a failure at bind time disables the
  WLED device and leaves the show running. The same change closes a validator
  gap: `[wled]` was checked by `--doctor` and by nothing else.
