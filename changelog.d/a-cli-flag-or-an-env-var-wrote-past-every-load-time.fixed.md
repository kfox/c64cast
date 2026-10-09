- A CLI flag or an env var wrote past every load-time validator. All ten fired
  at parse time, one layer *below* the last layer that writes, so `--system
  nonsense` or a blank `--audio-device` reached the run unchecked and failed
  mid-show. `merge_cli` re-runs the battery on the final config.
