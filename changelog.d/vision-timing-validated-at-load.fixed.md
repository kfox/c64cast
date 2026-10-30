- **A `[vision]` timing c64cast cannot run with is refused when the config
  loads.** `poll_interval_s = 0`, or a `nan` or `inf` `gesture_dwell_s`, used
  to stop the show at startup with a Python traceback; a negative or `nan`
  `poll_interval_s` ran hand tracking flat out; and a `nan` cooldown or hold
  left gestures that never fired. `poll_interval_s` now has to be a positive
  number and the other three timings finite, and `--doctor` names the field.
