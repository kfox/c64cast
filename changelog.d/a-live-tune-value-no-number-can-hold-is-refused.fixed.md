- **A web-console live-tune value that is not a usable number is refused.** A
  `NaN`, `Infinity`, `true` or list value used to set the knob to the top of its
  range or do nothing while reporting success, and an integer too large for a
  float answered `POST /perf/command` with a 500. Each now answers
  `{"ok": false}` and leaves the knob where it was.
