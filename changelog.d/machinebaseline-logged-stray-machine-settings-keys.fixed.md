- `machine_baseline()` logged stray machine-settings keys inline instead of
  collecting them, so under `--doctor` one stray key produced both a bare
  warning above the formatted report — the presentation the collect-then-present
  split exists to avoid — and a report row, with the dedupe unable to help
  because the escaping record never entered the list.
