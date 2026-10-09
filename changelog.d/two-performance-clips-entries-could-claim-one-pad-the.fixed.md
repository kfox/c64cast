- Two `[[performance.clips]]` entries could claim one pad. The loader checked
  `slot` uniqueness but only range-checked `pad`, and
  `midi_control._add_clip_pad_mappings` skips a `(kind, number)` it has already
  bound — so the second clip was simply unfirable, with no message. A repeat is
  now refused, naming both slots. (A collision *across* systems at that call
  site stays deliberate.)
