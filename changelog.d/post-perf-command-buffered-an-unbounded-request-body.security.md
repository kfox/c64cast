- **`POST /perf/command` buffered an unbounded request body.** `await
  request.json()` accumulates every chunk before parsing, and this was the one
  POST in the package that did not route through the shared cap that exists for
  precisely this — a remote memory exhaustion on a 1-2 GB appliance, taking down
  a process that owns live hardware, from a caller who needs no credential in
  the open mode. Capped at 64 KiB (a console command is a few hundred bytes),
  with a 413 for an oversized body and a 400 for one that is not a JSON object.
