- **A launcher scene on `input_source = "kernal"` now times out when the
  program leaves the keyboard buffer full.** Input used to mean "different
  from the first reading", so a program that never drains the buffer looked
  like a player at the keys for the rest of the run. A keyboard reading now
  counts only when it changes between polls; a joystick direction or fire
  button still counts for as long as it is held.
