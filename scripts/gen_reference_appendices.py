#!/usr/bin/env python3
"""Generate the Programmer's Reference Guide's appendices from the code.

    make reference-appendices          # rewrite them
    make reference-appendices && git diff --exit-code    # the drift guard

Appendices A-I are the exhaustive tables -- every config field, every scene
key, every overlay parameter, every CLI flag. Written by hand they would be
wrong within a release, so they are read out of the same model that already
answers ``--describe``, ``--compat`` and ``--print-schema``:
:mod:`c64cast.introspect`. An appendix cannot disagree with the program.

The output is committed Markdown, not Typst, for two reasons. The books are
rendered to GitHub Pages from these same sources, so nothing may live only in
the PDF; and ``scripts/build_book.py`` is deliberately stdlib-only (the release
workflow runs it under ``uv run --no-project``) while this script imports
``c64cast`` and everything it drags in. Keeping them separate means the release
never has to resolve the project environment to build a book.

Both facts about the output are load-bearing:

  * every file carries ``generated: true`` in its front matter, which is how
    :func:`main` finds the ones it owns and how a human editing one is warned;
  * the prose is written for *both* renderers -- it has to survive
    ``build_book.py``'s deliberately small Markdown subset (which rejects what
    it cannot translate rather than dropping it) and still read correctly on
    github.com. :func:`cell` and :func:`prose` below are what make help text
    written for a terminal safe in a table.

Adding an appendix means adding one entry to :data:`APPENDICES`.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from c64cast import cli as climod
from c64cast import doctor, effects, generators, introspect
from c64cast import paths as pathsmod

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "docs" / "reference"
CARD_DIR = REPO_ROOT / "docs" / "card"

# A default longer than this is summarised rather than printed. Only one field
# hits it -- [midi_control].cc_map, whose shipped default is two dozen mappings
# and 2,500 characters. A table cell is the wrong place to read that; the
# pointer next to it is a better answer than a wall that pushes the column out.
_MAX_DEFAULT = 56


# ---------------------------------------------------------------------------
# Markdown-safe text
# ---------------------------------------------------------------------------
#
# Help strings are written for `--describe` on a terminal, where nothing is
# markup. Two of those habits are hostile to a Markdown table, and both are
# fixed here rather than by rewording config.py: the help a reader sees in the
# book should be the help the program prints.

# The optional `-x/` prefix takes a short flag and its long form as one run:
# help text writes the pair as `-u/--url`, and backticking only the second half
# gives `-u/`--url``, which is safe but reads like a typo. The lookbehind keeps
# a run from starting mid-token -- `---` and a trailing `foo--bar` are not flags.
_FLAG_RE = re.compile(r"(?<![\w`-])((?:-[A-Za-z]/)?--[a-z][a-z0-9-]*)")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")


def _outside_code(text: str, fn: Callable[[str], str]) -> str:
    """Apply `fn` to the parts of `text` that are not already code spans.

    Some help strings already mark their own identifiers up (`[performance]`'s
    does), and a substitution inside an existing span would put backticks in
    the rendered output instead of around it.
    """
    parts = _CODE_SPAN_RE.split(text)
    return "".join(part if i % 2 else fn(part) for i, part in enumerate(parts))


def prose(text: str) -> str:
    """Help text, safe as Markdown body copy.

    A bare `--flag` is the one real hazard: Typst reads `--` as an en dash, so
    `build_book.py` rejects it outside a code span rather than render the wrong
    glyph. Backticks are also what the flag should have been wearing on GitHub,
    so this fixes the rendering and the markup in one move.
    """
    return _outside_code(" ".join(text.split()), lambda s: _FLAG_RE.sub(r"`\1`", s))


def cell(text: str) -> str:
    """Help text, safe as the body of a table cell.

    Only the flag handling: pipe escaping is :func:`table`'s job, because a
    cell is assembled from more than help text and every part of it needs the
    same treatment.
    """
    return prose(text)


def code(text: str) -> str:
    """A literal in a code span, with a fence long enough to contain it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def fmt_default(value: object) -> str:
    """A field's default, as a code span -- or a summary when it is enormous."""
    if value is introspect._REQUIRED:
        return "*(required)*"
    text = repr(value)
    if len(text) <= _MAX_DEFAULT:
        return code(text)
    if isinstance(value, list):
        return f"*{len(value)} shipped entries*"
    return code(text[:_MAX_DEFAULT] + "…")


def first_sentence(text: str) -> str:
    """The opening sentence of a docstring, collapsed onto one line.

    A period only ends a sentence when whitespace follows *and* what precedes
    it is not an abbreviation, which keeps `e.g.` and `i.e.` from cutting a
    description in half.
    """
    flat = " ".join(text.split())
    for match in re.finditer(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bcf)\.(?=\s|$)", flat):
        return flat[: match.end()]
    return flat


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """A GFM table. Emitted only when it has rows -- `build_book.py` needs the
    alignment row to recognise one at all, and a header with no body would
    render as a lone box.

    Every cell is pipe-escaped here rather than by its caller, because a pipe
    arrives from two directions: help text quoting its choices as
    `'cc'|'note'|'pc'`, and a type as ordinary as `str | None`. Missing either
    silently splits the row and the build stops on the cell count.
    """
    body = [[c.replace("|", r"\|") for c in r] for r in rows]
    if not body:
        return []
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return out + [""]


# ---------------------------------------------------------------------------
# The two senses of "live"
# ---------------------------------------------------------------------------
#
# Two different powers wear the same word. Appendix F's targets move under a
# MIDI knob, a pad or the web console mid-show; `apply="live"` is the metadata
# the on-C64 menu builds its panel from (`overlays/menu.py`). One mark for both
# would read as one power, so they are worded apart here and the introduction's
# Notation section says which is which.

# Configuration field -> the live-tune target that moves it. Keyed by
# `(section, field)`, where the section `scenes` is a `[[scenes]]` key.
#
# Written out rather than matched on the bare name: `[color].dither` is
# `mode.dither_method`, so a name match would miss it -- and it would mark
# `[audio].dither`, which is a 4-bit DAC's noise shaping and has nothing to do
# with the display pipeline. tests/test_reference_appendices.py resolves both
# sides of every entry.
_LIVE_TUNABLE: dict[tuple[str, str], str] = {
    ("color", "auto_fit_strength"): "mode.auto_fit_strength",
    ("color", "dither"): "mode.dither_method",
    ("color", "dither_strength"): "mode.dither_strength",
    ("color", "color_match"): "mode.color_match",
    ("color", "cell_strategy"): "mode.cell_strategy",
    ("color", "motion_smoothing"): "mode.motion_smoothing",
    ("scenes", "palette_mode"): "mode.palette_mode",
}


def marks(section: str, fd: introspect.FieldDoc) -> str:
    """The *live-tunable* and *menu-live* marks a field earns, if any.

    Appended to the description rather than stacked into the identity column:
    the identity is what a thing is called, and a mark is something it can do.
    """
    bits = []
    target = _LIVE_TUNABLE.get((section, fd.name))
    if target:
        bits.append(f"*Live-tunable* while a show runs, as {code(target)} — Appendix F.")
    if fd.apply == "live":
        bits.append("*Menu-live*: the on-C64 menu offers this knob, applied to the running scene.")
    return " ".join(bits)


def describe(section: str, fd: introspect.FieldDoc) -> str:
    """A field's description cell: its help, its choices, then its marks."""
    parts = []
    if fd.help:
        parts.append(cell(fd.help))
    if fd.choices:
        parts.append("Choices: " + ", ".join(code(c) for c in fd.choices) + ".")
    mark = marks(section, fd)
    if mark:
        parts.append(mark)
    return " ".join(parts)


def identity(*lines: str) -> str:
    """The left column of a fields table: what the thing is called, stacked.

    A name, its type and its default are three facts about one setting, not
    three columns of a grid -- and set as three columns on a 6.24in page they
    left the description, the only part written for a human, about a third of
    the measure and four words to a line. Stacking them puts the width back
    where the prose is. `<br>` is GFM's only line break inside a cell, and it
    is what github.com renders too.

    The first line is emboldened because it is the name, and a name is the only
    thing anybody scans a reference for. Left plain, the three lines are one
    undifferentiated block of mono and the eye has to read the type to work out
    that it was not the name.
    """
    kept = [line for line in lines if line]
    if not kept:
        return ""
    return "<br>".join([f"**{kept[0]}**", *kept[1:]])


def fields_table(label: str, rows: Iterable[Sequence[str]]) -> list[str]:
    """A two-column table: :func:`identity` on the left, prose on the right.

    The directive is an HTML comment, invisible on github.com, that tells
    `build_book.py` to hand this table the one column width every table of this
    shape uses -- so a scene key, an overlay parameter and a CLI flag all line
    up down the book instead of each being sized to its own longest entry.
    """
    body = table([label, "Description"], rows)
    return ["<!-- table: fields -->", *body] if body else []


def front_matter(number: str, title: str, blurb: str) -> list[str]:
    """The header every generated chapter opens with.

    `generated: true` is the marker :func:`main` deletes by and a human is
    warned by; the visible line under the title says the same thing to somebody
    reading the PDF, who cannot see front matter at all.
    """
    return [
        "---",
        f"number: {number}",
        "generated: true",
        "---",
        "",
        f"# {title}",
        "",
        "*Generated from the code by `scripts/gen_reference_appendices.py`.",
        "Edits here are overwritten; run `make reference-appendices`.*",
        "",
        blurb,
        "",
    ]


# ---------------------------------------------------------------------------
# Appendix A -- configuration sections and fields
# ---------------------------------------------------------------------------


def appendix_config() -> list[str]:
    sections = introspect.config_sections()
    total = sum(len(s.fields) for s in sections)
    out = front_matter(
        "A",
        "Configuration Sections",
        f"Every section of a configuration file: {len(sections)} sections and {total} "
        "fields, with the type each takes and the value it holds when you say nothing. "
        "A field a knob can move mid-show says so, and names the target Appendix F "
        "lists it under. `c64cast --describe section:NAME` prints any one of these at "
        "the terminal.",
    )
    for section in sections:
        out += [f"## `[{section.name}]`", ""]
        if section.help:
            out += [prose(section.help), ""]
        rows = [
            [
                identity(code(fd.name), code(fd.type), fmt_default(fd.default)),
                describe(section.name, fd),
            ]
            for fd in section.fields
        ]
        out += fields_table("Field", rows)
    return out


# ---------------------------------------------------------------------------
# Appendix B -- scene types
# ---------------------------------------------------------------------------


def appendix_scenes() -> list[str]:
    types = introspect.scene_types()
    # A field carried by every type is a property of scenes in general, not of
    # any one of them. Printing all six ten times would bury the handful of
    # keys that actually distinguish a waveform scene from a launcher.
    common = [
        fd for fd in types[0].fields if all(fd.name in {f.name for f in t.fields} for t in types)
    ]
    common_names = {fd.name for fd in common}

    out = front_matter(
        "B",
        "Scene Types",
        f"The {len(types)} kinds of scene a `[[scenes]]` block can be, and the keys each "
        "one reads. A key marked *live-tunable* can be moved by a knob mid-show; one "
        "marked *menu-live* is one the on-C64 menu can change without rebuilding the "
        "scene. `c64cast --describe scene:NAME` prints any one of these at the terminal.",
    )
    out += ["## Keys Every Scene Takes", ""]
    out += [
        prose(
            "These apply whatever the scene's `type` is. The per-type sections below "
            "list only what is particular to that type."
        ),
        "",
    ]
    out += fields_table(
        "Key",
        [
            [
                identity(code(fd.name), code(fd.type), fmt_default(fd.default)),
                describe("scenes", fd),
            ]
            for fd in common
        ],
    )

    for sd in types:
        # The name alone. `type = "webcam"` is how it is written in a file, but
        # as a heading it repeats the key ten times and reads as syntax where
        # the reader is scanning for a name.
        out += [f"## `{sd.name}`", ""]
        if sd.help:
            out += [prose(sd.help), ""]
        if sd.displays:
            modes = ", ".join(code(d) for d in sd.displays)
            out += [prose(f"Display modes: {modes}."), ""]
        rows = [
            [
                identity(code(fd.name), code(fd.type), fmt_default(fd.default)),
                describe("scenes", fd),
            ]
            for fd in sd.fields
            if fd.name not in common_names
        ]
        if rows:
            out += fields_table("Key", rows)
        else:
            out += [prose("No keys beyond the common ones above."), ""]
    return out


# ---------------------------------------------------------------------------
# Appendix C -- overlays
# ---------------------------------------------------------------------------


def appendix_overlays() -> list[str]:
    overlays = introspect.overlay_docs()
    total = sum(len(o.params) for o in overlays)
    out = front_matter(
        "C",
        "Overlays",
        f"The {len(overlays)} overlays and their {total} parameters. An overlay is "
        "attached to a scene with a `[[scenes.overlays]]` table; which ones a given "
        "display mode will accept is Appendix D.",
    )
    for od in overlays:
        out += [f"## `{od.name}`", ""]
        if od.help:
            out += [prose(od.help), ""]
        notes = []
        if od.requires_petscii:
            notes.append(
                "needs a text-capable mode"
                if od.supports_bitmap_text
                else "needs a PETSCII-compatible mode"
            )
        if od.requires_audio:
            notes.append("needs audio enabled")
        if od.compatible_modes:
            notes.append("only on " + ", ".join(code(m) for m in od.compatible_modes))
        if notes:
            out += [prose("Restrictions: " + "; ".join(notes) + "."), ""]
        out += fields_table(
            "Parameter",
            [
                [
                    identity(
                        code(p.name),
                        code(p.type) if p.type else "",
                        fmt_default(p.default),
                    ),
                    cell(p.help),
                ]
                for p in od.params
            ],
        )
    return out


# ---------------------------------------------------------------------------
# Appendix D -- the compatibility matrix
# ---------------------------------------------------------------------------


def appendix_compat() -> list[str]:
    modes, rows = introspect.compat_matrix()
    out = front_matter(
        "D",
        "Overlay and Display-Mode Compatibility",
        "Which overlays attach to which display modes. A ✓ works; a · is refused at "
        "configuration time rather than at the point it would have drawn. "
        "`c64cast --compat` prints this at the terminal.",
    )
    out += ["## The Matrix", ""]
    out += table(
        ["Overlay", *[code(m.name) for m in modes]],
        [[code(ov.name), *["✓" if ok else "·" for ok in oks]] for ov, oks in rows],
    )
    out += ["## Why a Cell Is Refused", ""]
    out += [
        prose(
            "A gap is one of three rules, not an accident of implementation. Text "
            "overlays need somewhere to put characters; a few overlays are written "
            "against one mode's memory layout; and an overlay that reads the audio "
            "stream needs the audio stream to exist."
        ),
        "",
    ]
    reasons = []
    for ov, oks in rows:
        if all(oks):
            continue
        first_gap = next(m for m, ok in zip(modes, oks, strict=True) if not ok)
        _, why = introspect.overlay_mode_ok(ov, first_gap)
        reasons.append([identity(code(ov.name)), cell(why)])
    out += fields_table("Overlay", reasons)
    return out


# ---------------------------------------------------------------------------
# Appendix E -- generators and effects
# ---------------------------------------------------------------------------


def _live_params(holder: str, cls: type) -> list[str]:
    """What a knob can reach on this generator or effect, one per line.

    A line each rather than one run of commas: they share the identity column
    with the name, and a generator with four of them would otherwise set as a
    paragraph of mono in a column narrower than the paragraph.

    Each is written with its holder — `source.speed`, not `speed` — because
    that is the string a `cc_map` entry has to carry, and printing the bare
    name made the reader translate a table into Appendix F's spelling to use
    it.
    """
    params: dict[str, tuple[float, float]] = getattr(cls, "LIVE_PARAMS", {}) or {}
    choices: dict[str, tuple[str, ...]] = getattr(cls, "LIVE_CHOICES", {}) or {}
    bits = [f"{code(f'{holder}.{name}')} {lo:g}–{hi:g}" for name, (lo, hi) in params.items()]
    bits += [
        f"{code(f'{holder}.{name}')} ({len(values)} values)" for name, values in choices.items()
    ]
    return bits


def appendix_generators() -> list[str]:
    out = front_matter(
        "E",
        "Generators and Effects",
        f"The {len(generators.REGISTRY)} procedural sources a `generative` scene can "
        f"draw from, and the {len(effects.REGISTRY)} effects that can be layered over "
        "any scene. Each entry lists what a knob can reach while the show is running "
        "under its name, spelled as the `target` a `param` mapping takes — so a line "
        "here can be copied into a `cc_map` unchanged. Appendix F is the same targets "
        "the other way round: one row each, with every generator or effect that "
        "declares it.",
    )
    out += ["## Generators", ""]
    out += [
        prose(
            "Set one as a `generative` scene's `source`. Every generator renders at "
            "320×200 and is downsampled by whichever display mode is in force."
        ),
        "",
    ]
    out += fields_table(
        "Generator",
        [
            [
                identity(code(name), *_live_params("source", cls)),
                cell(first_sentence(cls.__doc__ or "")),
            ]
            for name, cls in generators.REGISTRY.items()
        ],
    )
    out += ["## Effects", ""]
    out += [
        prose(
            "Named by a scene's `effect`, or chained in order with `effects`. An "
            "effect transforms the frame after the source has drawn it and before "
            "the display mode quantises it."
        ),
        "",
    ]
    out += fields_table(
        "Effect",
        [
            [
                identity(code(name), *_live_params("effect", cls)),
                cell(first_sentence(cls.__doc__ or "")),
            ]
            for name, cls in effects.REGISTRY.items()
        ],
    )
    return out


# ---------------------------------------------------------------------------
# Appendix F -- live-tune targets
# ---------------------------------------------------------------------------


def _live_target_rows(targets: Sequence[introspect.LiveTargetDoc]) -> list[list[str]]:
    rows = []
    for t in targets:
        if t.kind == "scalar":
            span = f"{t.lo:g} – {t.hi:g}" if t.lo is not None and t.hi is not None else ""
        else:
            span = ", ".join(code(c) for c in t.choices)
        rows.append([code(t.target), t.kind, span, ", ".join(code(o) for o in t.owners)])
    return rows


def appendix_live_targets() -> list[str]:
    targets = introspect.live_targets()
    out = front_matter(
        "F",
        "Live-Tune Targets",
        f"The {len(targets)} parameters a MIDI knob, pad or web-console control can "
        "move while a show is running. Each is the `target` string of a `param` "
        "action in `[[midi_control.cc_map]]`. A knob sweeps a scalar or bucket-"
        "selects a choice; a pad steps a choice on.",
    )
    for group in dict.fromkeys(t.group for t in targets):
        out += [f"## {group}", ""]
        out += table(
            ["Target", "Kind", "Range or values", "Declared by"],
            _live_target_rows([t for t in targets if t.group == group]),
        )
    return out


# ---------------------------------------------------------------------------
# Appendix G -- command-line flags
# ---------------------------------------------------------------------------


def appendix_cli() -> list[str]:
    parser = climod.build_parser()
    out = front_matter(
        "G",
        "Command-Line Flags",
        "Every option `c64cast` accepts, in the groups `-h` prints them in. A flag "
        "given here beats the same setting in a configuration file, which beats "
        "machine settings, which beats the built-in default.",
    )
    for group in parser._action_groups:
        actions = [a for a in group._group_actions if a.help != argparse.SUPPRESS]
        if not actions:
            continue
        out += [f"## {group.title.title() if group.title else 'Options'}", ""]
        rows = []
        for action in actions:
            names = ", ".join(code(s) for s in action.option_strings) or code(
                str(action.metavar or action.dest)
            )
            # A switch takes nothing, and a second line saying so under every
            # one of them is a column of em dashes the reader has to look past.
            if action.nargs == 0 or not action.option_strings:
                takes = ""
            elif action.choices:
                takes = ", ".join(code(str(c)) for c in action.choices)
            else:
                takes = code(str(action.metavar or action.dest.upper()))
            rows.append([identity(names, takes), cell(action.help or "")])
        out += fields_table("Flag", rows)
    return out


# ---------------------------------------------------------------------------
# Appendix H -- packaged example configurations
# ---------------------------------------------------------------------------


def appendix_examples() -> list[str]:
    paths = pathsmod.example_config_paths()
    out = front_matter(
        "H",
        "Example Configurations",
        f"The {len(paths)} runnable configurations that ship inside the package. Run "
        "one with `c64cast --config example:NAME`, or copy it out to edit with "
        "`c64cast --print-example NAME > c64cast.toml`. Each summary is read from the "
        "file's own header comment.",
    )
    out += ["## The Demos", ""]
    out += [
        prose(
            "A demo tagged *needs your own media* points at `assets/`, which ships "
            "empty because the material would be somebody else's. Drop a file in or "
            "repoint the scene's `file` before running it."
        ),
        "",
    ]
    rows = []
    for path in paths:
        summary = cell(introspect.example_summary(path))
        if introspect.example_needs_media(path):
            summary += " *(needs your own media)*"
        rows.append([identity(code(pathsmod.example_name(path))), summary])
    out += fields_table("Name", rows)
    return out


# ---------------------------------------------------------------------------
# Appendix I -- optional install extras
# ---------------------------------------------------------------------------


def _extra_requirements() -> dict[str, list[str]]:
    """`[project.optional-dependencies]`, read out of pyproject.toml.

    Not out of installed metadata: a checkout that was never installed still
    has to be able to build the book, and `importlib.metadata` would answer for
    whatever version happens to be on the machine rather than for this tree.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["optional-dependencies"]


def appendix_extras() -> list[str]:
    requirements = _extra_requirements()
    # `doctor._EXTRAS` already pairs each extra with the module that has to
    # import and a line on what it buys -- it is what `--doctor` probes with,
    # so an appendix built from it says what the program says.
    # tests/test_packaging_metadata.py holds it to the pyproject key set.
    extras = sorted(doctor._EXTRAS)
    out = front_matter(
        "I",
        "Optional Extras",
        f"The {len(extras)} groups of dependency that a plain install leaves out, what "
        "each one unlocks, the module `c64cast --doctor` imports to tell you it is "
        "there, and the packages it brings with it.",
    )
    out += ["## The Extras", ""]
    out += [
        prose(
            "Extras do not accumulate. Installing `c64cast[midi]` over `c64cast[video]` "
            "leaves you with MIDI and no video, so the install worth asking for is "
            "`c64cast[all]` — or `uv sync --all-extras` from a checkout. `c64cast "
            "--doctor` says which of these are importable and which are missing."
        ),
        "",
    ]
    rows = []
    for name, module, used_for in extras:
        installs = ", ".join(code(req) for req in requirements[name])
        rows.append([identity(code(name), code(module)), f"{cell(used_for)}. {installs}."])
    out += fields_table("Extra", rows)
    return out


# ---------------------------------------------------------------------------
# The performance card's live-target table
# ---------------------------------------------------------------------------


# The noun a holder's declarers are counted in, for :func:`declared_by`.
_OWNER_NOUNS: dict[str, str] = {
    "mode": "modes",
    "effect": "effects",
    "source": "generators",
    "scene": "scenes",
}


def declared_by(target: introspect.LiveTargetDoc, totals: dict[str, int]) -> str:
    """Who owns a live target, short enough for the card's column.

    Appendix F prints the owners; a 3.5in column cannot — `source.speed` has
    fourteen of them. But the fact itself is what a performer needs at the
    console, because a target the running scene does not declare is a silent
    no-op: a knob on `source.ring_freq` moves nothing at all unless `moire2`
    is the generator on screen.

    So the sole owner when there is one, and otherwise a count in the group's
    own noun — `14 generators` — which is what the question actually is at that
    point: is this knob worth a hand, or is it for one specific look.
    """
    if len(target.owners) == 1:
        return code(target.owners[0])
    if len(target.owners) == totals.get(target.holder):
        return "all"
    return f"{len(target.owners)} {_OWNER_NOUNS[target.holder]}"


def _holder_totals() -> dict[str, int]:
    """How many classes each holder has in total, so :func:`declared_by` can
    say `all` rather than a count that happens to equal the registry."""
    totals: dict[str, int] = {}
    for entry in introspect._iter_live_holders():
        holder = entry[0]
        totals[holder] = totals.get(holder, 0) + 1
    return totals


def card_live_targets() -> list[str]:
    """The card's most drift-prone page, generated in the same pass as Appendix F.

    Deliberately not the same table: a card is read at arm's length in a dark
    room, so it carries the target, its range and — compressed by
    :func:`declared_by` — who declares it.

    The provenance line is likewise shorter than an appendix's. `generated:
    true` is what the drift check and a human editor go by; the visible line is
    for somebody holding the printed card, who is better served by being told
    what the list *is* than how it was made.
    """
    targets = introspect.live_targets()
    totals = _holder_totals()
    out = [
        "---",
        "generated: true",
        "---",
        "",
        "# Live Targets",
        "",
        "*Generated from the code: every parameter a `param` mapping can name.*",
        "",
    ]
    for group in dict.fromkeys(t.group for t in targets):
        out += [f"## {group}", ""]
        rows = []
        for t in (x for x in targets if x.group == group):
            span = (
                f"{t.lo:g} – {t.hi:g}"
                if t.kind == "scalar" and t.lo is not None and t.hi is not None
                else f"{len(t.choices)} values"
            )
            rows.append([code(t.target), span, declared_by(t, totals)])
        out += table(["Target", "Range", "Declared by"], rows)
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# filename -> builder. The numeric prefixes continue the book's chapter
# sequence; `build_book.py` orders by them and reads the letter off front
# matter, so a gap here is a missing appendix rather than a renumbering.
APPENDICES: dict[Path, Callable[[], list[str]]] = {
    REFERENCE_DIR / "20-appendix-a-configuration.md": appendix_config,
    REFERENCE_DIR / "21-appendix-b-scene-types.md": appendix_scenes,
    REFERENCE_DIR / "22-appendix-c-overlays.md": appendix_overlays,
    REFERENCE_DIR / "23-appendix-d-compatibility.md": appendix_compat,
    REFERENCE_DIR / "24-appendix-e-generators-effects.md": appendix_generators,
    REFERENCE_DIR / "25-appendix-f-live-targets.md": appendix_live_targets,
    REFERENCE_DIR / "26-appendix-g-cli-flags.md": appendix_cli,
    REFERENCE_DIR / "27-appendix-h-examples.md": appendix_examples,
    REFERENCE_DIR / "28-appendix-i-extras.md": appendix_extras,
    CARD_DIR / "02-live-targets.md": card_live_targets,
}


def render(build: Callable[[], list[str]]) -> str:
    """One file's text: trailing blanks collapsed, exactly one final newline."""
    lines = build()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="report which files would change; write nothing (exit 1 if any would)",
    )
    args = ap.parse_args()

    stale: list[Path] = []
    for path, build in APPENDICES.items():
        text = render(build)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    rel = [str(p.relative_to(REPO_ROOT)) for p in stale]
    if args.check:
        if stale:
            print("stale, run `make reference-appendices`:", file=sys.stderr)
            for name in rel:
                print(f"  {name}", file=sys.stderr)
            return 1
        print(f"{len(APPENDICES)} generated files are up to date")
        return 0

    print(f"wrote {len(stale)} of {len(APPENDICES)} generated files")
    for name in rel:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
