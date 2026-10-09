- **`--profile` re-printed every scene the run had ever played.** The periodic
  summary iterated every scene it had ever seen, so a 10-scene looping
  playlist printed 10 lines every interval, 9 of them the last 64 frames of
  scenes that had ended minutes earlier, with nothing in the line marking them
  stale — and the table grew without bound on a playlist whose scene names
  come from the media (a directory scene renames itself per file; a video
  scene prefers the file's own title tag). Only scenes that have rendered
  since their last line are printed now, and an idle scene's samples are
  dropped. Two smaller fixes in the same summary: the scene name is escaped
  and length-capped, so a newline inside a played file's title tag can no
  longer forge an extra record in `--log-file`; and a stage the summary
  doesn't recognize is printed after the known columns rather than measured
  and silently discarded.
