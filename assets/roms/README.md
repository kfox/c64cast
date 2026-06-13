# assets/roms/

C64 ROM dumps used by the **preview window** and **stream recorder** to
software-render what the Ultimate 64 is displaying. The Ultimate 64 itself
does not need these — they're only for the local mirror.

## Files this directory expects

- `characters.901225-01.bin` — the 4 KB CHARGEN ROM (only the first 2 KB,
  the uppercase/graphics charset, is currently used).

The default config (`[preview] charset_path`) points at
`../assets/roms/characters.901225-01.bin` relative to the working directory.

## Where to obtain

The CHARGEN ROM ships with VICE (`C64/chargen-901225-01.bin`), with the
Ultimate 64 firmware bundle, and with any C64 emulator. It is also dumped
into `$D000-$DFFF` (when the VIC's character ROM is banked in) on a real
C64.

A built-in 8×8 ASCII charset is generated at runtime if `charset_path` is
missing or unreadable — the preview will work but PETSCII graphics codes
will appear as blank cells.
