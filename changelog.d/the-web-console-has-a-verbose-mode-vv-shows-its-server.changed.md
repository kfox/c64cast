- **The web console has a verbose mode: `-vv` shows its server log, `-vvv`
  adds the access log.** uvicorn builds a process-global logging configuration
  from `uvicorn.Config.__init__`, so every server c64cast starts — `--serve`,
  the `[control]` plane, the WLED device — pinned its own loggers shut the
  moment it was constructed, whatever verbosity the run had asked for. There
  was no `-v` count that answered "what did the phone send us", only "what did
  we send to the Commodore". The servers now install no logging of their own,
  so uvicorn's records ride the program's root logger like every other line:
  `-vv` releases the server log (bind, startup, shutdown), and `-vvv` releases
  the access log — one line per asset a browser fetches, naming every URL
  requested — together with the WebSocket frame log, one line per frame in
  either direction for as long as a console stays connected. Both are
  firehoses, and so both sit behind the same third `v` as the timer reads. A
  default run and `-v` are unchanged: neither shows anything from uvicorn.

  Two consequences beyond the new lines. A uvicorn *error* — a bind failure,
  most likely — used to go straight to stderr through a handler of uvicorn's
  own, so it never reached `--log-file` and never passed the secret-redacting
  formatter on the way; it now does, at every verbosity, alongside the line
  c64cast already logged beside it. And an access line is formatted by c64cast
  rather than by uvicorn: `uvicorn.access: 127.0.0.1:53124 - "GET /api/state
  HTTP/1.1" 200` in place of uvicorn's colored `200 OK`, the status phrase
  being the one field in the second and not the first, and in exchange the
  line picks up the timestamp and level columns every other record has.
