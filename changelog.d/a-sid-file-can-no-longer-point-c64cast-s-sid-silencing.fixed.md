- **A `.sid` file can no longer point c64cast's SID-silencing writes at
  arbitrary I/O chips.** A PSID v3/v4 header declares its extra SID chips as
  one byte each, decoded as `$D000 | byte << 4`. That arithmetic always lands
  inside `$D010-$DFF0`, so the range check meant to reject a malformed byte
  could never fire and *every* nonzero byte named a chip — including `$C0`,
  which put a "SID" on CIA #1, where the waveform scene's 25-byte teardown
  write stops the jiffy IRQ and the keyboard scan until the machine is
  physically reset. Extra-SID bytes are now validated against the windows the
  PSID spec actually permits (even bytes resolving to `$D420-$D7E0` or
  `$DE00-$DFE0`), and a byte outside them degrades to single-SID the way the
  code always claimed it did. Chip bases are also de-duplicated: two chips
  declared at the same — or overlapping — address used to let the later one
  silently take over the earlier one's register shadow, leaving that chip's
  scope window flat for the whole tune while the audience heard it play.
