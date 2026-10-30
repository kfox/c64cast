- **Every web-console session now gets the SID hardware warnings.** The
  one-time warnings about an assumed SID model, the C64's own output playing
  the wrong model, and a multi-SID tune on a single-SID machine were said once
  per process, so under `c64cast serve` only the first session heard them. They
  are now said once per machine connection.
