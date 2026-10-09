- **`--upgrade` could kill an install partway through and leave a broken
  one.** Every install command ran under a 120-second ceiling, and
  `subprocess.run` SIGKILLs the child when that expires — so a `uv sync
  --all-extras` or `pip install --upgrade c64cast` resolving a release that
  moved an `opencv-python`/PyAV/numpy pin (≈100 MB to download, or a source
  build on a Pi-class host with no matching wheel) was killed while
  replacing `site-packages`, by the one command whose purpose is repairing
  an install, with no flag or variable to raise the limit. The ceiling is
  now an hour and `$C64CAST_UPGRADE_TIMEOUT_S` overrides it (`0` removes it
  entirely); a command that does hit it is sent SIGINT first — the signal
  uv/pip/pipx/git already unwind cleanly from — and killed only if it
  ignores that; and the message says the upgrade may be only partly applied
  and names the variable. The read-only `git status` probe keeps its own
  short timeout, since it mutates nothing.
