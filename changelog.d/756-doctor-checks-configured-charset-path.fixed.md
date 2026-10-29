- **`--doctor` checks the character ROM a `charset_path` points at.** A
  `[preview]` or `big_text` `charset_path` is read ahead of the installed ROM,
  but the report only looked at the installed one, so it could call the ROM
  missing while the run used the configured file, or stay silent about a
  configured file that was missing or not a charset. Each configured path now
  gets its own row: its verdict, or the fallback a missing one leads to.
