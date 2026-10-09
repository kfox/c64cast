- **An Ultimate 64 SID map can no longer put an emulated core on top of a real
  chip.** Planning for a `.sid` file's own chip addresses claimed a physical
  socket first and then placed the UltiSID cores without knowing where that
  socket sat. Because the firmware aligns a split core's base *downward*, a
  four-chip tune whose first address matched the socketed chip's model enabled
  the socket at `$D400` and put a half-split core over `$D400` too — the tune
  playing on the real chip and the emulation at once, audible as a detuned
  double, which is the one state this planner exists to prevent. Core placement
  now refuses a window that covers a claimed socket, and gives the socket up
  rather than the map when no split level clears it. Core bases are also bounded
  above, against the firmware's own address enum, instead of only below.
