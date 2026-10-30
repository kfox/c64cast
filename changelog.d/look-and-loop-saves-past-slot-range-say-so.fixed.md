- **A look or loop saved to a slot past 250 is no longer logged as saved.** A
  web console look save, or a MIDI `look_save` / `loop_slot` pad mapped to a
  slot above 250, was dropped by the preset store while the log reported it
  saved. The log now warns that the slot is out of range and nothing was
  saved.
