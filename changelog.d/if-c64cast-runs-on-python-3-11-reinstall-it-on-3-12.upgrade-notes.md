- **If your c64cast runs on Python 3.11, reinstall it on a newer Python to
  get this release.** `uv tool upgrade c64cast` and `pipx upgrade c64cast`
  keep the interpreter the tool was installed with, so on 3.11 they report
  nothing to upgrade. Use `uv tool upgrade --python 3.12 c64cast` (uv fetches
  that Python if it is missing), or `pipx reinstall --python python3.12
  c64cast`.
