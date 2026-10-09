- **A still picture now repaints after a dropped Ultimate DMA connection.**
  When the connection to an Ultimate was reset with writes still unconfirmed
  (the DMA service switched off, for instance), or a write failed outright on
  any device, the parts of the picture those writes
  carried stayed wrong until they next changed, because c64cast remembered
  them as sent. It now forgets everything it sent after such a loss, and the
  next frame redraws the whole picture. While the machine stays unreachable,
  c64cast now waits between reconnect attempts, from half a second up to
  8 seconds, instead of trying again on every write; against a switched-off
  Ultimate each attempt could stall the picture and the audio for up to
  5 seconds.
