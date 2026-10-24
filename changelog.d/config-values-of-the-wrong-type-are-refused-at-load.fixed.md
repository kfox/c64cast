- **A config value of the wrong type is refused at load, naming the key.** A
  quoted number such as `[audio] sample_rate = "12000"`, a float where an
  integer belongs, or `true` where a number belongs used to be accepted and
  fail later wherever it was first used; `--doctor` crashed with a
  `TypeError` on the quoted sample rate. Every scalar key in every section,
  including `[[scenes]]`, `[[performance.clips]]`, machine settings and an
  ensemble's master and per-system files, is now checked when the file loads.
  An integer is still accepted wherever a float is.
