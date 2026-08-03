# How to Read This Book

The *User's Guide* teaches c64cast in order, from a first run to a playlist you
would show somebody. This book does not teach it. It is the volume you open
when you already know what you want and need to know exactly what it is called,
what it accepts, and what it does when you say nothing at all.

It is organised by subsystem, not by audience. The musician driving a SID and
the VJ driving a club wall read the same chapter on sound, because it is the
same sound path. Where a subject belongs to two chapters it is written once and
referred to from the other.

## What Is In Here

Chapters 1 to 6 are prose: the rules of the configuration language, the
vocabulary of scenes and overlays, the display pipeline from frame to VIC-II
register, the sound path, the link into the Commodore's memory and what lands
there, and everything that reaches c64cast from outside or leaves it.

Chapter 7 is the exception, and is for a different reader: it is what you need
to add a scene, an overlay, a generator or an effect to c64cast itself. Nothing
in the first six chapters depends on it.

Appendices A to H are not prose and are not written by hand. They are generated
from the same definitions the program answers `--describe`, `--compat` and
`--print-schema` from, by `scripts/gen_reference_appendices.py`, and they are
regenerated as part of the build. A table in this book cannot disagree with the
program it documents; if it ever does, the build is broken and says so.

Appendix I is a glossary, which is hand-written because a machine has no
opinion about which words a reader will not know.

## What Is Not

Three things live outside this book on purpose.

`docs/architecture.md` and the notes it indexes are the contributor's account
of *why* each module is built the way it is, including the approaches that were
tried and abandoned. Read it before changing a module.

This book does explain itself where a default would otherwise look arbitrary,
and it prints the measurement that settled the matter when there is one — a
sampler clock rate, a frame budget, how much louder one companding curve is
than another. What it leaves to `architecture.md` is the history: which other
approaches were tried, and why they lost.

`docs/caveats.md` records the hardware's own limits, which are frequently the
real answer to "why can't it just". `docs/troubleshooting.md` is organised by
symptom, which is the right index when something is wrong and the wrong one
when you are designing.

## Notation

A name in `this face` is something you type or something the program prints:
a configuration key, a command-line flag, a file, a value. A section of a
configuration file is written with its brackets, as `[audio]`, and a repeated
table with its double brackets, as `[[scenes]]`, exactly as TOML spells them.
A key inside a particular section is qualified when there is any doubt:
`[audio].backend`.

Values are shown as TOML writes them, so a string carries its quotes and a
number does not. Where a key takes one of a fixed set of values, the set is
listed in full; where it takes a free string, the shape of that string is
given by example.

A parameter marked *live* can be moved while a show is running, by a MIDI
knob, a pad, or the web console. Appendix F lists every one of them.

A command-line flag is written as it is typed, with its leading dashes, as
`--config`. Flags are introduced beside the behaviour they change rather than
catalogued in the prose; Appendix G is the full list, in the groups `-h`
prints them in.

## Where a Setting Comes From

Every value c64cast uses is resolved through one ladder, and every layer beats
the ones below it. Stated once, formally, for a single system:

1. The built-in default, which is the value in the dataclass field.
2. Machine settings, from `~/.config/c64cast/settings.toml` — the connection
   target, capture devices and SID model this particular computer should
   assume when nothing says otherwise.
3. The configuration file: the one named by `--config`, else `./c64cast.toml`
   if it exists.
4. Command-line flags.
5. The environment, which today is only `C64CAST_DMA_PASSWORD`, and which is
   the environment precisely so that a password never reaches shell history or
   a process listing.

An ensemble run inserts one layer. Each system resolves defaults, then machine
settings, then its own configuration file; then the master file's cascade fills
in only those fields still sitting at the machine-overlaid baseline, so a
per-system file always beats the master that gathered it; then flags and the
environment as before.

This ladder is the whole of the rule. Chapter 1 works through what each layer
is for and how to see which one supplied a given value.

> [!NOTE]
> Quick playback — naming media files directly, as `c64cast clip.mp4` — builds
> its configuration in memory and never writes one, but it climbs the same
> ladder. In particular it applies machine settings, which is why it can find
> your Commodore without a `-u` flag.
