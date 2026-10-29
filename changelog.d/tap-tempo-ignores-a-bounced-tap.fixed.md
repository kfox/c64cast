- **A double-fired tap no longer skews the tapped tempo.** A pad that bounced,
  or a console tap sent twice, landed as an extra beat a few milliseconds
  after the real one and pulled the averaged BPM far off (four taps at 120 BPM
  plus one bounce read as 160). A tap closer than one beat at 400 BPM to the
  previous one is now ignored.
