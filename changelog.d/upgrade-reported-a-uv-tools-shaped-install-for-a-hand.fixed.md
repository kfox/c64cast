- `--upgrade` reported a `uv/tools`-shaped install for a hand-made pip venv
  that merely sat under a directory called `tools` beside one called `uv`
  (likewise `pipx`/`venvs`), and printed the wrong installer's command:
  those segments are now matched as adjacent path components, which is what
  the documentation always said. `--upgrade` also launches the binary
  `shutil.which` resolved rather than handing the unqualified name back to
  `exec` to re-resolve `PATH`, and the login MOTD's staleness line says no
  update check has *succeeded* in over 30 days rather than blaming PyPI for
  not answering — on a machine with no timer the last attempt did answer and
  nobody has asked since, so the old wording sent an admin hunting a network
  fault that did not exist.
