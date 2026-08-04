---
number: J
---

# Glossary

Terms this book uses without stopping to explain them.

## The Machine

**Bitmap mode** — a display mode addressing individual pixels rather than
characters. Costs far more memory traffic per frame than a character mode, and
buys detail no arrangement of characters can reach.

**C64U** — the Commodore 64 Ultimate, and the name this book uses for the
whole Ultimate platform: the C64U itself, the earlier Ultimate 64 and Ultimate
64 Elite boards, and — with the exceptions noted where they matter — the
Ultimate II+ cartridge. They function the same way and speak the same
protocols, which is why one name covers them. `u64://` is the connection
scheme for all of them, named after the board the scheme was written against.

**Character mode** — a display mode drawing the screen from the 8×8 glyphs in
the character ROM, one byte per cell. Cheap, and the only place text overlays
can put text without help.

**Character ROM** — the Commodore's own glyph shapes. c64cast reads them off
your machine on the first run so that a preview window renders in the same face
the Commodore will.

**DMA** — direct memory access. The route c64cast writes to the Commodore's
memory by, over a socket rather than the REST interface, because REST cannot
carry writes at a useful rate.

**REU** — the RAM Expansion Unit. Additional memory the Ultimate can emulate,
used here as a buffer deep enough to keep audio fed.

**SID** — the sound chip, and by extension a tune file written for it. Two
models exist, 6581 and 8580, and they do not sound alike.

**TeensyROM+** — the other supported backend, a cartridge reached over USB
serial or over the network. A narrower pipe than the C64U's, and the features
that depend on the wider one say so.

**VIC-II** — the video chip. Everything in the display pipeline exists to end
as values in its registers.

## The Software

**Backend** — the hardware c64cast is driving, and the transport it drives it
over. Selected by the scheme of a connection target.

**Display mode** — how a frame is turned into something the VIC-II can show.
Six of them; see Appendix D for what each will accept.

**Effect** — a transformation applied to a frame after its source has drawn it
and before the display mode quantises it. Several may be chained.

**Ensemble** — several Commodores driven from one run, each with its own
configuration, gathered by a master file.

**Extra** — an optional group of Python dependencies, named in square brackets
after the package: `c64cast[video]`. Each one unlocks a feature that would
otherwise log a line and be skipped. They do not accumulate, so installing one
by name removes the others; `c64cast[all]` is the install that has everything.

**Generator** — a procedural frame source: a scene that computes its picture
rather than reading one.

**Live parameter** — a parameter that can be moved while a show is running,
from a knob, a pad, or the console. Appendix F lists them all.

**Machine settings** — the per-computer overlay at
`~/.config/c64cast/settings.toml`, holding what this machine should assume when
nothing else says.

**Overlay** — something drawn over a scene rather than instead of it: a clock,
a logo, a spectrum.

**Quick playback** — naming media on the command line instead of writing a
configuration. Builds one in memory and runs it.

**Scene** — one item in a playlist: a source, a display mode, whatever overlays
it carries, and how long it lasts.

**Target** — the string naming a live parameter in a `param` mapping, as
`effect.decay`. Appendix F is the list.
