- Every field of `update_check.json` is now type-checked on read instead of
  coerced, because `float()`, `bool()` and `str()` cannot fail and so turned
  a mistyped field into a confident wrong answer rather than the routine
  "nothing recorded yet". `"newer": "false"` read back as `True` —
  `bool("false")` is truthy — and `rechecked()` could not correct it, since
  a record whose `running_version` already matches is returned untouched, so
  the login banner offered the release the box already ran. A `checked_at`
  of `NaN` or `Infinity` (both of which `json.loads` accepts as bare
  literals) made *every* comparison in `is_stale` read as "not stale", which
  disabled the one safeguard against quoting a dead answer — permanently and
  with no other symptom, so an internet-facing appliance that had missed a
  year of releases said nothing about it at either surface. `is_stale` now
  also treats a date more than a day in the future as stale rather than
  fresh: "can't tell how old this is" is not "recent".
