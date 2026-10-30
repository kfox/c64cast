- **A screen stream with no picture ends instead of holding a stream slot for
  good.** Watching the screen of an Ultimate 64 that never sent a frame, or
  one whose show ended while the screen was open, left a server thread waiting
  on that stream after the tab closed; four of those and the console could
  not show any screen again until c64cast restarted. The stream now ends once
  the machine has sent nothing for about four seconds.
