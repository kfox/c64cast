- **A MIDI Stop now holds the beat grid even when the device keeps sending
  clock.** Many drum machines and DAWs send `0xF8` clock while stopped; the grid
  restarted on the first of those pulses, so Stop did nothing. The phase now
  holds until a Start or Continue, while the BPM keeps following the clock.
