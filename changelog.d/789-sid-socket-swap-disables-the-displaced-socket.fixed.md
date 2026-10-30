- **A SID tune moved onto the other socket for its chip model no longer plays
  on the wrong chip.** When autoconfig swapped a socket onto the tune's address
  it left the socket already there enabled too, and when that was socket 2 the
  volume pass muted the chip autoconfig had picked. The displaced socket is now
  disabled for the tune and restored afterward.
