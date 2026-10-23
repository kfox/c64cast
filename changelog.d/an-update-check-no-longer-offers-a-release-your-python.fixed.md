- **An update check no longer offers a release your Python cannot install.**
  When a release needs a newer Python than the install runs on,
  `--check-for-updates`, `--doctor`, the web console's banner and
  `--motd-line` now name the Python it needs and the command that reinstalls
  c64cast on it, instead of pointing at `--upgrade`, which on that Python
  finds nothing to upgrade. (#702)
