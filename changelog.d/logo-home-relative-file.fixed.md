- **A `logo` overlay whose `file` starts with `~` now shows the art.** The
  existence check read the path unexpanded, so a file that was there drew the
  "LOGO PLACEHOLDER" box instead.
