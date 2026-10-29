- **A launcher scene on `input_source = "kernal"` now times out when the
  program leaves the keyboard buffer full.** Input used to mean "different
  from the first reading", so a program that never drains the buffer looked
  like a player at the keys for the rest of the run. Input now means a change
  since the previous poll; a key or direction held down counts once.
