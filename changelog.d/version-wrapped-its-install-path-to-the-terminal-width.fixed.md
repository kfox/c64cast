- **`--version` wrapped its install path to the terminal width.** argparse's
  stock version action renders through `HelpFormatter`, so the path — the half
  of that line that exists to be pasted into `uv tool upgrade` or read back to
  someone answering "I upgraded and it still reports the old version" — arrived
  split across lines on a narrow terminal, and inside a directory name once
  narrow enough. A deep enough install path reaches the same wrap at an
  ordinary width. `--version` now prints the line as composed, at any width.
