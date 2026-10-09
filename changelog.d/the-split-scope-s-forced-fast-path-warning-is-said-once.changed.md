- **The split scope's forced-fast-path warning is said once, not once per
  tune.** Configuring `persistence` or `scroll_columns` and then playing a
  multi-SID tune discards those modes — per-window scroll and echo are not
  built yet — and the log says so, which it should. But the sentence is fixed
  by the two knobs plus the chip count, so it is identical every time, while
  the reflow that triggers it is not a one-off: a playlist re-runs a scene each
  lap and the scope reflows per tune, so a 2SID-heavy playlist repeated the
  same warning indefinitely. It now warns the first time and leaves the
  repeats to `-v` for the rest of that scene's life, matching how the
  multi-SID downmix notice already behaves. A different scene still gets its
  own warning, since its user has not been told.
