- `--check-for-updates` and `--doctor` could traceback instead of reporting
  "couldn't check". `upgrade.latest_release` is documented "never raises",
  but a PyPI body decoding to a list, a string, a number, `null`, or
  `{"info": null}` raised `TypeError` from its subscript chain, and its lazy
  `import requests` sat outside the guard, so a half-installed `requests` —
  the state an upgrade exists to fix — raised `ImportError` through it. A
  mis-shaped body could also produce a *fabricated* answer that nothing
  downstream could catch: `str()` turned `{"info": {"version": 5}}` into the
  release `"5"`, which compares newer than everything this project has
  published, and `{"info": {"version": null}}` into `"None"`, which cleared
  the recorded `unanswered_since` and discarded the previous real answer.
  The failure is now caught broadly and logged at debug, so `-v`
  distinguishes a DNS failure from a proxy's 403 from a shape change instead
  of collapsing all of them into the same silent `None`.
