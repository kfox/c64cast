- **A SID file could paint a system-mismatch arrow that was not there.** The
  oscilloscope's metadata row marks a clock mismatch with a `\x01` sentinel,
  swapped for a mirrored right-arrow glyph wherever it appears in the row, and
  the PSID/RSID copyright field reached that row as raw bytes. The three header
  text fields are now decoded as ISO-8859-1 with control characters replaced by
  spaces.
