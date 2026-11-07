- **A C64 with a VIC-II Kawari now gets its colors right automatically.**
  With `[hardware].host_palette = "auto"` on a TeensyROM+ or Ultimate II+,
  c64cast reads the RGB palette the Kawari is driving straight from its
  registers at startup, instead of assuming the classic VIC-II colors. The
  read leaves the Kawari's settings untouched. On a Kawari's composite output,
  set `host_palette = "pepto"`.
