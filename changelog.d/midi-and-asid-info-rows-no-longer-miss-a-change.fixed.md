- **The MIDI and ASID scenes' info rows no longer stick on a stale value.** A
  note, controller or ASID message that arrived while a row was being painted
  was dropped from the display until the next change, so the volume or play
  state shown could stay wrong indefinitely.
