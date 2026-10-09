- **`--log-file` now rotates, so a network peer can no longer choose how large
  it gets.** The destination was a plain `logging.FileHandler` with no size of
  its own, and not every record that reaches it is the operator's: one
  malformed TCP connection to the web console writes a 74-byte
  `Invalid HTTP request received.` line at every verbosity, and the console's
  own "refused" warning is request-driven as well. Measured over loopback, four
  different malformed shapes each cost that same 74 bytes at 3,309
  connections/s — 245 KB/s into the file, for as long as the run lasts. The
  handler is a `RotatingFileHandler` now: the file rotates at 4 MiB, four older
  copies are kept beside it as `run.log.1` … `run.log.4`, and the set stops at
  20 MiB. The size is set by what an ordinary run writes on its own —
  ~2.1 MiB/day for an unattended installation, so a normal run still ends
  without rotating — rather than by the flood. Holding uvicorn's logger at
  ERROR was the other candidate and is not the fix: it would silence genuine
  warnings at the default verbosity and leave every other root-bound record
  exactly as exposed.
