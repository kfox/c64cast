- `--upgrade` on a source tree with no `.git` — an unpacked release archive,
  which carries the `pyproject.toml` the checkout verdict is read from — now
  says so, instead of reporting "could not be checked (is git on PATH?)" and
  "Commit or stash first" and sending the user off to fix a `PATH` that was
  never the problem in a directory holding no repository. The checkout
  branch also refuses up front when `uv` is missing rather than discovering
  it after `git pull` has already moved the source, which used to leave the
  tree on new code with the old dependency set; and an unverifiable tree now
  gets its own wording rather than borrowing the dirty tree's advice.
