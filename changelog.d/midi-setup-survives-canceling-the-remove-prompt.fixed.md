- **Canceling the "Remove which index?" prompt in `--midi-setup` no longer
  crashes the wizard.** Pressing Ctrl-C at that prompt raised a `TypeError`
  and lost every mapping learned so far. It now returns to the review list.
