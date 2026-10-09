- **The web console answers a too-deeply-nested request like any other bad
  one.** The same body sent to the web console's API or the `/perf` command
  route got a 500 and an error traceback in the log instead of a 400, and as a
  frame on either console socket it closed that socket — the console's only
  feed for session state and log lines. Both now treat it as JSON that does not
  decode.
