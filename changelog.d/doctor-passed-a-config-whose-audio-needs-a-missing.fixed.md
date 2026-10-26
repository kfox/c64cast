- **`--doctor` passed a config that cannot start without `sounddevice`.** Audio
  is on by default, and a run with audio on refuses to start when the `mic`
  extra is missing, but `--doctor` only listed the missing extra as a warning
  and exited 0. Each system with audio on now gets an error naming the fix:
  install the extra, or run with `--no-audio`.
