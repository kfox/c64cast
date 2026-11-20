- **REU-staged bitmap video on a TeensyROM+ no longer kills the audio or
  drops the C64 to READY after a few minutes.** The TeensyROM+ can lose a
  C64 write to a REU register, and a frame copy aimed by a lost write ran
  past its video bank over the audio player. The C64 now reads each REU
  register back after writing it and writes it again until it holds.
