- A misspelled config *table* vanished without a diagnostic. Unknown *keys*
  were only ever found inside tables the loader applies, so `[hardwear]` or
  `[ultimate65]` produced nothing at all; a whole unrecognized table is now
  collected like a stray key, with a "did you mean" of its own, and `--doctor`
  renders it as a table rather than a key.
