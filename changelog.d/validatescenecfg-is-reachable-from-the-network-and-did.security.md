- **`validate_scene_cfg` is reachable from the network, and did unbounded
  filesystem work there.** The load-time SID header check read its whole
  candidate file with no guard, so a config naming a FIFO `x.sid` blocked the
  validate request thread forever and a multi-gigabyte one exhausted memory; it
  now requires a regular file and caps the read. A `**` glob whose first path
  segment is itself a pattern under `/` — a walk of every mounted volume inside
  one HTTP request — is refused outright. A general depth or time bound on glob
  expansion is still open; the ceiling trades against legitimately deep HVSC and
  media trees.
