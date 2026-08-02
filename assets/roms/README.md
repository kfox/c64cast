# assets/roms/

**You almost certainly don't need this directory.** c64cast reads the C64
character ROM out of the machine you're connected to on the first run and caches
it under your data dir — `~/.local/share/c64cast/roms/chargen.bin`
(`$XDG_DATA_HOME`-aware; `%LOCALAPPDATA%\c64cast\roms\` on Windows;
`$C64CAST_DATA_DIR` overrides). Nothing to find, nothing to download.

```bash
c64cast --dump-char-rom -u u64://192.168.2.64    # re-read it explicitly
c64cast --install-char-rom /path/to/chargen.bin  # use a dump you already have
c64cast --doctor --skip-probe                    # which ROM is in use, and is it sound
```

`--install-char-rom` is the fallback for a setup c64cast can't read from (an
emulator-only rig, or a TeensyROM on firmware older than v0.7.2.5, which has
neither the memory read nor the IRQ-enabled idle the dump needs). It accepts a
2 KB or 4 KB dump and needs no hardware.

## What the ROM is for

Every glyph c64cast draws as C64 text: the text overlays on bitmap modes
(`scrolling_text`, `marquee`, `corner_text`, `logo`), `big_text`'s 8×-scaled
scroller, the on-C64 menu, the oscilloscope's labels, and the preview window +
stream recorder, which turn screen-code bytes back into 8×8 pixel cells. Without
one, c64cast substitutes a built-in ASCII font — readable, but not the C64 font,
and PETSCII graphics codes come out blank. See
[docs/usage.md](../../docs/usage.md#the-character-rom).

## This directory

A legacy location, still honoured last in the resolution order so an existing
source checkout with a dump at `characters.901225-01.bin` keeps working. Nothing
writes here; new dumps go to the data dir. No ROM bytes are tracked in this
repo, and none ship in the sdist, the wheel, or a release asset — only this
README is committed.
