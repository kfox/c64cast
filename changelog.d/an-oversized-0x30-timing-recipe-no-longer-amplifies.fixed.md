- **An oversized `0x30` timing recipe no longer amplifies every later frame or
  silently deletes a SID chip.** The recipe is a SID write order, so it can be
  no longer than the register table and can name each register once — but
  neither bound was enforced, and the recipe persists until the next `0x30`. A
  400 KB SysEx message decoded to 200,000 entries and turned an ordinary
  four-register frame into 200,004 write ops, per frame, on the MIDI reader
  thread. The ops past what a slot holds were then dropped in silence, and
  because a multi-SID slot packs the chips in order, the ones that vanished
  belonged to the later chips: a two-chip tune lost its second chip's frame
  entirely with nothing logged. The decoder now caps the recipe and keeps a
  repeated register at its first position, and a slot that has to truncate says
  so.
