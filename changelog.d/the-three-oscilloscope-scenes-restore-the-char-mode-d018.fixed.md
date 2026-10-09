- **The three oscilloscope scenes restore the char-mode `$D018` they claim to.**
  `AsidScene`, `MidiScene` and `WaveformScene` each said they put the default
  `$D018` back for the next scene's char mode and then wrote their own hires
  value (`$18`), leaving the VIC's matrix pointer on the bitmap layout. Harmless
  today because the next scene engages its own display mode — but a false claim
  a maintainer could act on. All three now write `$14` —
  `VIC.D018_CHAR_DEFAULT`, matrix at bank+`$0400` with the bitmap bit clear.
