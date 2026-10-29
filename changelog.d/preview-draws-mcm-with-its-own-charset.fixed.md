- **The preview window and the recording now draw an MCM scene as the C64
  shows it.** They drew every text cell with the character ROM, but MCM video
  points the VIC at its own block charset, so the mirror showed letter shapes
  where the screen showed color blocks. Text modes now follow the charset and
  screen matrix `$D018` selects.
