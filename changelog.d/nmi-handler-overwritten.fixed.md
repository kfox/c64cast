- **c64cast now notices when the C64's audio player has been overwritten.**
  A stray write over the player at `$C020` used to leave the audio silent
  while c64cast kept running and logged nothing. When the audio stops, the
  check for a crashed C64 now also compares the player's bytes, so the scene
  ends with an error and the machine is put back.
