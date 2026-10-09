- The `/perf` console page moved out of `perf_console.py` into a packaged
  `c64cast/control/perf_console.html`. No behavior change — the same bytes are
  served, read once at first request — but 650 lines of HTML/CSS/JS in a Python
  string had no syntax highlighting, no formatter, no linter, and no way to
  open in a browser while iterating on it. Deliberately not folded into the
  Node build that produces the Svelte console: being one self-contained
  document with no build step is what makes this the surface that works when
  the bundle was never built.
