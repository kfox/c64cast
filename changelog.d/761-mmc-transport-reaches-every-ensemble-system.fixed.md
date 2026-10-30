- **An MMC transport command now reaches every system in an ensemble.** A MIDI
  Machine Control SysEx (play, stop, record, …) carries no channel, so it used
  to reach only the first system; it now goes to all of them, as the MIDI clock
  does.
