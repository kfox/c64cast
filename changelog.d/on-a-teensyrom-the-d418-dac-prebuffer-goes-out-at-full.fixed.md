- **On a TeensyROM+, the `$D418` DAC prebuffer goes out at full speed.** The
  link slices writes only while the NMI player runs, but it was told the
  player was running before the timer was armed, so the whole prebuffer went
  out in slices with no NMI to spare. It now hears once the arm has taken.
