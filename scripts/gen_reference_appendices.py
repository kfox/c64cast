#!/usr/bin/env python3
"""Generate the Programmer's Reference Guide's appendices and index from the code.

    make reference-appendices          # rewrite them
    make reference-appendices && git diff --exit-code    # the drift guard

Appendices A-I are the exhaustive tables -- every config field, every scene
key, every overlay parameter, every CLI flag. Written by hand they would be
wrong within a release, so they are read out of the same model that already
answers ``--describe``, ``--compat`` and ``--print-schema``:
:mod:`c64cast.app.introspect`. An appendix cannot disagree with the program.

The index is the same model read the other way round, crossed with the book's
own prose: every name the program can utter, against the sections that discuss
it. See :func:`build_index`.

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
import functools
import importlib.util
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from c64cast.app import cli as climod
from c64cast.app import doctor, introspect
from c64cast.app import paths as pathsmod
from c64cast.scenes import effects, generators

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "docs" / "reference"
CARD_DIR = REPO_ROOT / "docs" / "card"


def _script_module(name: str) -> ModuleType:
    """A sibling module under ``scripts/``, loaded by path.

    The index writes a link per locator, and a link resolves only if the anchor
    it names is spelled exactly the way the converter spells it. Borrowing
    ``heading_slug`` rather than reimplementing GitHub's rule a second time is
    what stops the two from drifting into a book full of dead links.

    scripts/ is not a package, so neither ``import`` nor ``sys.path`` can be
    relied on -- this module is loaded by path itself, from the tests. An
    already-loaded copy is reused rather than a second one built.
    """
    module = sys.modules.get(name)
    if module is None:
        path = Path(__file__).resolve().with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Registered before exec: @dataclass resolves annotations through
        # sys.modules[cls.__module__], which blows up if the module isn't there.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


# The dialect (slugs, front matter, chapter discovery) and the Typst renderer
# that measures a listing against the page it will be set on.
bd = _script_module("bookdoc")
bb = _script_module("build_book")

# A default longer than this is summarized rather than printed. Only one field
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
    if value is introspect.REQUIRED:
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
    alignment row to recognize one at all, and a header with no body would
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


def typed(fd: introspect.FieldDoc | introspect.ParamDoc) -> str:
    """A setting's identity: its name, then its type and default, each named.

    The two lines under the name were bare -- ``str`` over ``'serial'`` -- and
    which was which was only obvious to somebody who already knew. They are
    three facts of different kinds stacked in one column, so each says what
    kind it is; the labels are prose rather than mono, which also stops the
    block from reading as one unbroken run of code.
    """
    return identity(
        code(fd.name),
        f"*Type:* {code(fd.type)}" if fd.type else "",
        f"*Default:* {fmt_default(fd.default)}",
    )


# ---------------------------------------------------------------------------
# Worked fragments
# ---------------------------------------------------------------------------
#
# A table of settings says what each one means and nothing about where it is
# written, which leaves a reader who has found the right knob still holding a
# name and no file. Every appendix that documents something configurable opens
# its section with the two or three lines that put it in a file.
#
# The fragments are generated from the same model as the tables under them, so
# a renamed field cannot leave a stale example behind it. Values are defaults,
# never invented: a snippet showing `file = "clip.mp4"` would be the one line
# on the page that the program had never agreed to.

# How wide a fragment's lines may be before the PDF wraps them. Borrowed from
# `build_book.py`, which is where the measure is derived and where the test
# that holds every listing in every book to it reads the number from.
CODE_WIDTH = bb.CODE_WIDTH["guide"]

# Settings per fragment. Enough to show the shape; past this it stops being an
# illustration and starts being a configuration file the reader has to read.
_SNIPPET_KEYS = 4


def toml_literal(value: object) -> str | None:
    """`value` as TOML, or None when a fragment is better off without it.

    A fragment exists to show placement, so it carries only settings whose
    default *is* a usable value. An empty string, a `None` and a required field
    are all the same case -- the program has no answer, and the alternative is
    to invent one.
    """
    if value is introspect.REQUIRED or value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return f'"{value}"' if value else None
    return None


def snippet(
    header: str,
    rows: Sequence[tuple[str, str, str]],
    *,
    indent: str = "",
    required: Sequence[str] = (),
) -> list[str]:
    """A fenced TOML fragment: a table header and some `key = value` lines.

    Choices ride along as a trailing comment, aligned into a column, which is
    both the house style of the packaged examples and the one thing a reader
    writing a file wants next to the key. A comment that would push the line
    past the measure is dropped rather than wrapped -- it is a convenience,
    and the table below the fragment has the full list.

    A required key has no default, so it cannot be shown as a value without
    inventing one. It is named in a comment instead: leaving it out silently
    would make every fragment for an overlay like `big_text` a block that does
    not run, which is worse than a fragment that says what is missing.
    """
    if not rows and not required:
        return []
    body = [f"{indent}{key} = {value}" for key, value, _ in rows]
    width = max((len(line) for line in body), default=0)
    out = []
    for line, (_, _, comment) in zip(body, rows, strict=True):
        padded = f"{line.ljust(width)}  # {comment}" if comment else line
        out.append(padded if len(padded) <= CODE_WIDTH else line)
    # Last, not first: a note naming a key the lines above do not set reads as
    # a caption on the block rather than as a correction to it.
    if required:
        names = ", ".join(required)
        tail = "have no defaults" if len(required) > 1 else "has no default"
        out.append(f"{indent}# also required: {names} — {tail}")
    return ["```toml", f"{indent}{header}", *out, "```", ""]


def required_names(fields: Iterable[introspect.FieldDoc | introspect.ParamDoc]) -> list[str]:
    """The keys with no default, which a fragment has to name rather than set."""
    return [fd.name for fd in fields if fd.default is introspect.REQUIRED]


def sample_rows(
    fields: Iterable[introspect.FieldDoc | introspect.ParamDoc],
    *,
    first: Sequence[tuple[str, str, str]] = (),
    limit: int = _SNIPPET_KEYS,
) -> list[tuple[str, str, str]]:
    """The settings a fragment shows, in the order the program declares them.

    Declaration order and not alphabetical: `config.py` lists a section's most
    consequential field first, and a fragment is meant to be the beginning of a
    real file rather than the first four names in the alphabet.
    """
    rows = list(first)
    for fd in fields:
        if len(rows) >= limit:
            break
        value = toml_literal(fd.default)
        if value is None or any(row[0] == fd.name for row in rows):
            continue
        # Only a config field declares its choices; an overlay parameter states
        # them in its help, where a fragment cannot get at them cleanly.
        choices: Sequence[str] = getattr(fd, "choices", ())
        rows.append((fd.name, value, " | ".join(choices)))
    return rows


def scalar_range(lo: float, hi: float) -> str:
    """A live parameter's range, set as a literal.

    In the body face its digits stand a good deal taller than the mono name
    beside them -- Jost's figures reach 0.700em against Inconsolata's 0.623em
    -- and a column of ranges read as the largest thing in the table. As a
    literal the whole column is one face, which is also what it is: values.

    The dash keeps its spaces. `-2–2` is two characters of punctuation running
    together and reads as one glyph nobody can name; `-2 – 2` is a range with
    a negative low end.
    """
    return code(f"{lo:g} – {hi:g}")


def fields_table(
    label: str,
    rows: Iterable[Sequence[str]],
    *,
    description: str = "Description",
    directive: str = "table: fields",
) -> list[str]:
    """A two-column table: :func:`identity` on the left, prose on the right.

    The directive is an HTML comment, invisible on github.com, that tells
    `build_book.py` to hand this table the one column width every table of this
    shape uses -- so a scene key, an overlay parameter and a CLI flag all line
    up down the book instead of each being sized to its own longest entry.

    The heading and the directive are parameters only because the index is the
    same shape carrying locators rather than descriptions; every appendix takes
    both defaults.
    """
    body = table([label, description], rows)
    return [f"<!-- {directive} -->", *body] if body else []


def front_matter(number: str, title: str, blurb: str) -> list[str]:
    """The header every generated chapter opens with.

    `generated: true` is the marker :func:`main` deletes by and a human editing
    one is warned by. It stays in the front matter, which neither renderer
    prints: how this file came to exist is the repository's business, and a
    reader of the book is owed the appendix rather than a note about the build.
    """
    return [
        "---",
        f"number: {number}",
        "generated: true",
        "---",
        "",
        f"# {title}",
        "",
        blurb,
        "",
    ]


# ---------------------------------------------------------------------------
# Appendix A -- configuration sections and fields
# ---------------------------------------------------------------------------


def appendix_config() -> list[str]:
    # Alphabetical. `config_sections()` is in declaration order, which is the
    # order the annotated example file reads in and is a reasonable narrative;
    # an appendix is not read in order, it is looked things up in, and twenty
    # sections in an order the reader cannot predict means paging through all
    # of them to find `[wled]`.
    sections = sorted(introspect.config_sections(), key=lambda s: s.name)
    total = sum(len(s.fields) for s in sections)
    out = front_matter(
        "A",
        "Configuration Sections",
        f"Every section of a configuration file, in alphabetical order: {len(sections)} "
        f"sections and {total} fields, with the type each takes and the value it holds "
        "when you say nothing. A field a knob can move mid-show says so, and names the "
        "target Appendix F lists it under. Each section opens with a fragment showing "
        "how it is written; the table under it is the whole section. `c64cast "
        "--describe section:NAME` prints any one of these at the terminal.",
    )
    for section in sections:
        out += [f"## `[{section.name}]`", ""]
        if section.help:
            out += [prose(section.help), ""]
        out += snippet(
            f"[{section.name}]",
            sample_rows(section.fields),
            required=required_names(section.fields),
        )
        rows = [[typed(fd), describe(section.name, fd)] for fd in section.fields]
        out += fields_table("Field", rows)
    return out


# ---------------------------------------------------------------------------
# Appendix B -- scene types
# ---------------------------------------------------------------------------


def appendix_scenes() -> list[str]:
    # Alphabetical, for the reason Appendix A is; see the note there.
    types = sorted(introspect.scene_types(), key=lambda s: s.name)
    # A field carried by every type is a property of scenes in general, not of
    # any one of them. Printing all six ten times would bury the handful of
    # keys that actually distinguish a waveform scene from a launcher.
    takers: dict[str, list[str]] = {}
    for sd in types:
        for fd in sd.fields:
            takers.setdefault(fd.name, []).append(sd.name)

    def doc(name: str) -> introspect.FieldDoc:
        return next(fd for sd in types for fd in sd.fields if fd.name == name)

    common = [fd for fd in types[0].fields if len(takers[fd.name]) == len(types)]
    # A key that all but one type takes is a general property with an exception,
    # and an exception is a sentence. `duration_s` is a key of nine of the ten,
    # and printed in each of them it was sixty identical words nine times over.
    absentee: dict[str, str] = {
        name: next(sd.name for sd in types if sd.name not in who)
        for name, who in takers.items()
        if len(who) == len(types) - 1
    }
    common_names = {fd.name for fd in common} | set(absentee)

    out = front_matter(
        "B",
        "Scene Types",
        f"The {len(types)} kinds of scene a `[[scenes]]` block can be, in alphabetical "
        "order, and the keys each one reads. A key marked *live-tunable* can be moved "
        "by a knob mid-show; one marked *menu-live* is one the on-C64 menu can change "
        "without rebuilding the scene. `c64cast --describe scene:NAME` prints any one "
        "of these at the terminal.",
    )
    out += ["## Keys Every Scene Takes", ""]
    out += [
        prose(
            "These apply whatever the scene's `type` is. The per-type sections below "
            "list only what is particular to that type."
        ),
        "",
    ]
    # No fragment here. The common keys have no shape of their own -- a
    # `[[scenes]]` block is always one of the ten types below, and a fragment
    # written around this list would have to pick a `type` at random and print
    # it as though it were the general case.
    out += fields_table("Key", [[typed(fd), describe("scenes", fd)] for fd in common])

    for missing in dict.fromkeys(absentee.values()):
        names = [name for name, absent in absentee.items() if absent == missing]
        out += [prose(f"Every type but {code(missing)} takes these as well."), ""]
        out += fields_table(
            "Key", [[typed(doc(name)), describe("scenes", doc(name))] for name in names]
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
        own = [fd for fd in sd.fields if fd.name not in common_names]
        out += snippet(
            "[[scenes]]",
            sample_rows(own, first=[("type", f'"{sd.name}"', "")]),
            required=required_names(own),
        )
        if own:
            out += fields_table("Key", [[typed(fd), describe("scenes", fd)] for fd in own])
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
        # Indented two spaces, as the packaged examples write it: an overlay
        # table is nested inside the scene it decorates, and the indentation is
        # what says so at a glance in a file TOML itself reads flat.
        out += snippet(
            "[[scenes.overlays]]",
            sample_rows(od.params, first=[("type", f'"{od.name}"', "")]),
            indent="  ",
            required=required_names(od.params),
        )
        out += fields_table("Parameter", [[typed(p), cell(p.help)] for p in od.params])
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
    # By the rule and not by the overlay. A row each put "needs a text-capable
    # mode (petscii/blank/hires/mhires)" on the page ten times, which is the
    # same sentence read ten times to learn one thing; the reader who wants to
    # know about one overlay has the matrix above.
    reasons: dict[str, list[str]] = {}
    for ov, oks in rows:
        if all(oks):
            continue
        first_gap = next(m for m, ok in zip(modes, oks, strict=True) if not ok)
        _, why = introspect.overlay_mode_ok(ov, first_gap)
        reasons.setdefault(why[:1].upper() + why[1:], []).append(ov.name)
    out += table(
        ["Rule", "Overlays"],
        [[why, ", ".join(code(n) for n in names)] for why, names in reasons.items()],
    )
    return out


# ---------------------------------------------------------------------------
# Appendix E -- generators and effects
# ---------------------------------------------------------------------------


def _live_params(cls: type) -> list[str]:
    """What a knob can reach on this generator or effect, one per line.

    A line each rather than one run of commas: they share the identity column
    with the name, and a generator with four of them would otherwise set as a
    paragraph of mono in a column narrower than the paragraph.

    The holder is *not* repeated on every line. It was, so that a line could be
    copied into a `cc_map` unchanged -- but it is the same word on all fifty of
    them, and at 1.5in `source.scroll_speed` is the entry that decides the
    column's width for the sake of a prefix the reader already knows from the
    heading. The section above each table gives it once.
    """
    params: dict[str, tuple[float, float]] = getattr(cls, "LIVE_PARAMS", {}) or {}
    choices: dict[str, tuple[str, ...]] = getattr(cls, "LIVE_CHOICES", {}) or {}
    bits = [f"{code(name)} {scalar_range(lo, hi)}" for name, (lo, hi) in params.items()]
    bits += [f"{code(name)} {code(f'{len(values)} values')}" for name, values in choices.items()]
    return bits


# The names the two fragments in Appendix E are written around. Constants
# rather than "whatever the registry lists first", so the examples stay the
# ones worth showing; tests/test_reference_appendices.py resolves each against
# its registry, so a rename cannot leave a fragment naming nothing.
_SAMPLE_GENERATOR = "plasma"
_SAMPLE_EFFECTS = ("mirror", "trails")


def appendix_generators() -> list[str]:
    out = front_matter(
        "E",
        "Generators and Effects",
        f"The {len(generators.REGISTRY)} procedural sources a `generative` scene can "
        f"draw from, and the {len(effects.REGISTRY)} effects that can be layered over "
        "any scene. Each entry lists what a knob can reach under its name while the "
        "show is running. Appendix F is the same parameters the other way round: one "
        "row each, with every generator or effect that declares it.",
    )
    out += ["## Generators", ""]
    out += [
        prose(
            "Set one as a `generative` scene's `source`. Every generator renders at "
            "320×200 and is downsampled by whichever display mode is in force."
        ),
        "",
    ]
    out += snippet(
        "[[scenes]]",
        [
            ("type", '"generative"', ""),
            ("source", f'"{_SAMPLE_GENERATOR}"', "any generator below"),
        ],
    )
    out += [
        prose(
            "A parameter below is reached live as `source.NAME` — a knob on this "
            'scene\'s `source` is mapped with `target = "source.speed"`. It moves '
            "nothing unless the generator declaring it is the one on screen."
        ),
        "",
    ]
    out += fields_table(
        "Generator",
        [
            [identity(code(name), *_live_params(cls)), cell(first_sentence(cls.__doc__ or ""))]
            for name, cls in generators.REGISTRY.items()
        ],
    )
    out += ["## Effects", ""]
    out += [
        prose(
            "Named by a scene's `effect`, or chained in order with `effects`. An "
            "effect transforms the frame after the source has drawn it and before "
            "the display mode quantizes it."
        ),
        "",
    ]
    out += snippet(
        "[[scenes]]",
        [
            ("type", '"generative"', ""),
            ("source", f'"{_SAMPLE_GENERATOR}"', ""),
            ("effects", "[" + ", ".join(f'"{e}"' for e in _SAMPLE_EFFECTS) + "]", "in order"),
        ],
    )
    out += [
        prose(
            "A parameter below is reached live as `effect.NAME`, and applies to "
            "whichever effect in the chain declares it."
        ),
        "",
    ]
    out += fields_table(
        "Effect",
        [
            [identity(code(name), *_live_params(cls)), cell(first_sentence(cls.__doc__ or ""))]
            for name, cls in effects.REGISTRY.items()
        ],
    )
    return out


# ---------------------------------------------------------------------------
# Appendix F -- live-tune targets
# ---------------------------------------------------------------------------


def _live_target_rows(targets: Sequence[introspect.LiveTargetDoc]) -> list[list[str]]:
    """One row per target, named without its holder.

    The holder is the heading, as it is in Appendix E: every row of a section
    carries the same word, and `source.` is what decides the column's width for
    a prefix the reader has just read above the table.
    """
    rows = []
    for t in targets:
        if t.kind == "scalar":
            span = scalar_range(t.lo, t.hi) if t.lo is not None and t.hi is not None else ""
        else:
            span = ", ".join(code(c) for c in t.choices)
        # The kind is a literal too. Left in the body face it was the one
        # proportional column in a table of mono, and Jost against Inconsolata
        # at an equal size reads as a larger word -- so `scalar` was the
        # loudest thing in a row, which is not what a reader is looking for.
        name = t.target.rpartition(".")[2]
        rows.append([code(name), code(t.kind), span, ", ".join(code(o) for o in t.owners)])
    return rows


# The target the appendix's fragment is written around; a test resolves it.
_SAMPLE_TARGET = "effect.decay"

# What each holder is, in the words chapter 6's own holder table uses. The
# picker group (`LiveTargetDoc.group`) is a label for a tab -- "Effect" under a
# heading reading `effect` says nothing -- and a section that now carries half
# of every target under it owes the reader a sentence about what it holds.
# tests/test_reference_appendices.py checks the keys against the registries.
_HOLDER_GLOSS: dict[str, str] = {
    "mode": "The display mode's color pipeline",
    "effect": "An effect in the scene's chain",
    "source": "A generative scene's generator",
    "scene": "The scene itself",
}


def appendix_live_targets() -> list[str]:
    targets = introspect.live_targets()
    out = front_matter(
        "F",
        "Live-Tune Targets",
        f"The {len(targets)} parameters a MIDI knob, pad or web-console control can "
        "move while a show is running. Each names the `target` of a `param` action "
        "in `[[midi_control.cc_map]]`: the holder that heads its section, a dot, and "
        "the parameter. A knob sweeps a scalar or bucket-selects a choice; a pad "
        "steps a choice on.",
    )
    out += ["## Mapping One", ""]
    out += snippet(
        "[[midi_control.cc_map]]",
        [
            ("type", '"cc"', "cc | note | pc"),
            ("number", "13", "the controller number"),
            ("action", '"param"', ""),
            ("target", f'"{_SAMPLE_TARGET}"', "a heading and a row below"),
        ],
    )
    out += [
        prose(
            "A target is only live while something that declares it is on screen — "
            "the *Declared by* column is that list. A knob on a target the running "
            "scene does not declare moves nothing, silently."
        ),
        "",
    ]
    # Headed by the holder rather than by the group it is picked under, because
    # the heading is now carrying the half of every target the rows no longer
    # spell -- and `mode` is the word that has to be joined to `dither_strength`
    # to make one. The group name is the sentence under it.
    for holder in dict.fromkeys(t.holder for t in targets):
        mine = [t for t in targets if t.holder == holder]
        out += [f"## `{holder}`", ""]
        gloss = _HOLDER_GLOSS[holder]
        out += [prose(f"{gloss}. A row's target is {code(holder + '.')} and its name."), ""]
        out += table(
            ["Parameter", "Kind", "Range or values", "Declared by"], _live_target_rows(mine)
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
# The index
# ---------------------------------------------------------------------------
#
# Two halves, and both are mechanical. The terms come from the same
# introspection the appendices are built from, so the index cannot list a
# setting the program does not have. The locators come from the book's own
# Markdown, so it cannot point at a section that is not there.

INDEX_PATH = REFERENCE_DIR / "30-index.md"

# Words that arrive in a code span looking like terms and are not: every one of
# them is what you write on the *right* of an `=`. Left in, `auto` alone would
# collect a locator in five chapters and mean nothing in any of them.
_INDEX_STOP_WORDS = frozenset({"auto", "true", "false", "none", "on", "off", "random"})

# Under this length a token is an abbreviation the scan cannot tell from an
# accident -- `id`, `hz`, a short flag's single letter. `fps` sits just above.
_MIN_TERM_LEN = 3

# Locators per term. The fourth is never the one you wanted, and the column it
# has to fit in is about three inches.
_MAX_LOCATORS = 3


@dataclass(frozen=True)
class Term:
    """One index entry.

    `key` is what the scan has to find for the term to be entered; `display` is
    how the entry prints; `sort` is what it files under, which is neither --
    `--config` files under C and ``[audio]`` under A. `qualifier` is the holder
    a dotted name was split from, and only orders the run of entries that share
    a `sort`: bare `dither` before `dither (audio)` before `dither (color)`.
    """

    key: str
    display: str
    sort: str
    qualifier: str = ""


@dataclass(frozen=True)
class Locator:
    """One section a term was found in, and where it ranks among the rest."""

    filename: str
    slug: str
    title: str
    chapter: str
    order: tuple[int, int, int, int]

    def markdown(self) -> str:
        """The locator as a link at the section that discusses the term.

        On github.com this is the locator: the Markdown is the book, there are
        no pages, and a section title is the only thing to point at. In the PDF
        the same link is set as the page it lands on -- see `pagerefs` in
        docs/shared/template.typ, which the `table: index` directive routes it
        through. One source, and neither renderer carries a locator its reader
        cannot follow.
        """
        where = f" ({self.chapter})" if self.chapter else ""
        return f"[{locator_text(self.title)}{where}]({self.filename}#{self.slug})"


_CODE_SPAN_SCAN_RE = re.compile(r"(?P<fence>`+)(?P<body>[^`]+)(?P=fence)")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_LONG_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
_QUALIFIED_KEY_RE = re.compile(r"\[(\w+)\]\.(\w+)")
_SECTION_BRACKET_RE = re.compile(r"\[\w+\]")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")


def mentions(text: str) -> set[str]:
    """Every string in one line of Markdown that could be naming something.

    Only what is inside a code span counts. The book writes every name it means
    in `this face`, and matching bare prose would file the sentence "the video
    scene plays a file" under `video`, `scene` and `file` at once.

    A span is picked apart rather than matched whole because one span carries
    several names: ``[audio].backend`` is both the qualified key and the bare
    field, and `mode.dither_strength` is a live target and a field.

    A bracketed section is taken whole and then masked out, so ``[audio].backend``
    stops crediting the *scene* key `audio` — which is a different setting, and
    was collecting the whole of the `[audio]` section's mentions.
    """
    found: set[str] = set()
    for span in _CODE_SPAN_SCAN_RE.finditer(text):
        body = span.group("body").strip()
        found.add(body)
        found.update(_LONG_FLAG_RE.findall(body))
        for m in _QUALIFIED_KEY_RE.finditer(body):
            found.add(f"{m.group(1)}.{m.group(2)}")
        for m in _IDENT_RE.finditer(_SECTION_BRACKET_RE.sub(" ", body)):
            token = m.group(0)
            found.add(token)
            found.add(token.rpartition(".")[2])
    return found


def sort_key(text: str) -> str:
    """What an entry files under: the word a reader would look it up by.

    Leading punctuation goes because nobody looks for `--config` under a
    hyphen, and a leading article goes because "The Audio Slot" is an entry
    about the audio slot.
    """
    return _LEADING_ARTICLE_RE.sub("", text.strip("`[]-").lower())


def term_for(key: str) -> Term:
    """One program name, as the entry a reader would look it up by.

    A dotted name is split and inverted -- `effect.axis` files under *axis* and
    prints ``axis (effect)``. Filed whole it went under E with forty-nine other
    targets, and a reader who knows the parameter is called `axis` and not
    which holder declares it had nowhere to start. Inverting is what a printed
    index has always done with a qualified term, and it puts `axis (effect)`
    next to any other `axis` the program has.

    A bracketed section keeps its brackets, because they are how the book
    writes it and how the reader will type it, and files under the bare word.
    """
    holder, dot, name = key.rpartition(".")
    if dot and not key.startswith("--"):
        return Term(key, f"{code(name)} ({holder})", sort_key(name), holder)
    return Term(key, code(key), sort_key(key))


def code_terms() -> dict[str, Term]:
    """Every name the program can utter, keyed by what a code span would say.

    A configuration field is entered bare, and additionally qualified when two
    sections both have a key by that name -- which is the rule the book's own
    Notation section states, and the only case where the qualified spelling
    tells the reader anything. `dither` is `[color]`'s dithering and `[audio]`'s
    noise shaping, so both are listed; `agc` belongs to `[dsp]` alone, and a
    `dsp.agc` row would point at the same place the `agc` row does.

    A name that two registries share -- a scene type and a generator called the
    same thing -- is one entry, since one entry is what it is.
    """
    terms: dict[str, Term] = {}

    def add(key: str) -> None:
        if len(key) < _MIN_TERM_LEN or key.lower() in _INDEX_STOP_WORDS:
            return
        terms.setdefault(key, term_for(key))

    sections = introspect.config_sections()
    shared: dict[str, int] = {}
    for sd in sections:
        for fd in sd.fields:
            shared[fd.name] = shared.get(fd.name, 0) + 1
    for sd in sections:
        add(f"[{sd.name}]")
        for fd in sd.fields:
            if shared[fd.name] > 1:
                add(f"{sd.name}.{fd.name}")
            add(fd.name)
    for st in introspect.scene_types():
        add(st.name)
        for fd in st.fields:
            add(fd.name)
    for od in introspect.overlay_docs():
        add(od.name)
        for p in od.params:
            add(p.name)
    for md in introspect.display_modes():
        add(md.name)
    for name in (*generators.REGISTRY, *effects.REGISTRY):
        add(name)
    for lt in introspect.live_targets():
        add(lt.target)
    for action in climod.build_parser()._actions:
        for flag in action.option_strings:
            if flag.startswith("--"):
                add(flag)
    return terms


# Plain-language entries, hand-picked.
#
# The index used to enter every section title in the prose chapters, and it was
# the wrong half of the book: a title is a topic, topics are what the contents
# page is for, and nobody has ever looked up "One Surface for the Whole
# Ensemble". What a reader looks up is a *word* -- "camera", not "Choosing a
# Camera" -- and arrives at every section that discusses it.
#
# Kept short and deliberately basic. Each has to be a word somebody would try
# who does not yet know what c64cast calls the thing; anything the program
# spells itself is already in `code_terms` and is skipped below rather than
# entered twice.
#
# Each maps the entry as it prints to the stem the prose is searched for, which
# are the same word most of the time. They part where the book only ever
# inflects it -- nothing says "double buffering", but "double-buffered" is
# everywhere -- and an entry that reads as a noun should not have to be spelled
# as the participle to find itself.
_CONCEPTS: dict[str, str] = {
    "beat grid": "beat grid",
    "camera": "camera",
    "character ROM": "character ROM",
    "clip grid": "clip grid",
    "color RAM": "color RAM",
    "companding": "compand",
    "dirty cache": "dirty cache",
    "display mode": "display mode",
    "dithering": "dithering",
    "double buffering": "double buffer",
    "frame rate": "frame rate",
    "gesture": "gesture",
    "jukebox": "jukebox",
    "microphone": "microphone",
    "oscilloscope": "oscilloscope",
    "page flip": "page flip",
    "quantization": "quantiz",
    "raster interrupt": "raster interrupt",
    "screen RAM": "screen RAM",
    "single-scene mode": "single-scene mode",
    "subtune": "subtune",
    "vblank": "vblank",
    "web console": "web console",
    "write budget": "write budget",
}


def concept_pattern(stem: str) -> re.Pattern[str]:
    """What counts as a mention of a plain-language term.

    Case-insensitive, word-bounded so "camera" is not found inside a longer
    word, and tolerant of the endings English puts on a stem -- the book writes
    "display modes" as often as "display mode", and they are one entry. A space
    in the stem also matches a hyphen, which is the only difference between
    "page flip" and half the sentences that discuss one.
    """
    body = r"[ -]".join(re.escape(word) for word in stem.split())
    return re.compile(rf"\b{body}(?:e?s|ed|ing|ation)?\b", re.IGNORECASE)


def concept_terms(codes: dict[str, Term]) -> dict[str, Term]:
    """:data:`_CONCEPTS`, minus any the program already spells for itself.

    `[playlist]` is a section and `playlist` would be a second entry filed at
    the same letter pointing at much the same places, which is a duplicate
    wearing different type rather than a second way in.
    """
    taken = {term.sort for term in codes.values()}
    return {
        name: Term(name, name, sort_key(name)) for name in _CONCEPTS if sort_key(name) not in taken
    }


def scan(
    paths: Sequence[Path],
    codes: dict[str, Term],
    concepts: dict[str, Term],
) -> dict[str, list[Locator]]:
    """Where each term is discussed: the best few, in the order they are read.

    Best is a section *titled* with the term, because that is the one written
    about it; then the prose chapters before the appendices, which is what
    keeps the locator for `dither` from being its own row in Appendix A's
    `[color]` table rather than the section explaining what dithering is for.
    The two rules do not fight: an appendix names a term in a heading only
    where the whole section is that term's entry, and every other appendix hit
    is a table cell, which sorts last.

    The few that survive are then put back into document order. Relevance is
    how they are *chosen* and would be the better order if they were titles,
    but they print as page numbers, and "152, 41, 84" reads as a fault in the
    index rather than as a ranking nobody can see.
    """
    hits: dict[str, dict[tuple[str, str], Locator]] = {}
    patterns = {name: concept_pattern(_CONCEPTS[name]) for name in concepts}
    for file_order, path in enumerate(paths):
        fields, body, _ = bd.parse_front_matter(path.read_text(encoding="utf-8"), path)
        chapter = fields.get("number") or ""
        prose_rank = 0 if chapter.isdigit() else 1
        slugs = iter(bd.file_section_slugs(body))
        section: tuple[str, str] | None = None
        fenced = False
        for lineno, line in enumerate(body.split("\n")):
            if line.strip().startswith("```"):
                # An example configuration names half the program. Indexing
                # what a listing happens to contain would bury the discussion.
                fenced = not fenced
                continue
            if fenced:
                continue
            heading = bd._HEADING_RE.match(line)
            if heading is not None and len(heading.group("hashes")) in (2, 3):
                in_title = True
                section = (next(slugs), heading.group("text").strip())
                text = section[1]
            elif section is not None:
                in_title = False
                text = line
            else:
                continue
            found = mentions(text)
            # A plain-language term is matched in the prose itself, not in a
            # code span: it is in the index precisely because the program never
            # says it. Spans are blanked first so `dithering` is credited to
            # the sentence about dithering and not to every `dither` in a table.
            bare = _CODE_SPAN_SCAN_RE.sub(" ", text)
            found |= {term for term, pattern in patterns.items() if pattern.search(bare)}
            slug, title = section
            # Appendix A writes a field bare inside the section it belongs to,
            # so `dither` under `## [color]` is where `color.dither` is
            # defined. Offering the qualified spelling here is what gives the
            # qualified entries their table locator; a slug that is not a
            # section name forms nothing the term table will match.
            found |= {f"{slug}.{name}" for name in found}
            for name in found:
                term_doc = codes.get(name) or concepts.get(name)
                if term_doc is None:
                    continue
                order = (0 if in_title else 1, prose_rank, file_order, lineno)
                where = hits.setdefault(term_doc.key, {})
                where.setdefault((path.name, slug), Locator(path.name, slug, title, chapter, order))
    return {
        key: sorted(
            sorted(found.values(), key=lambda loc: loc.order)[:_MAX_LOCATORS],
            key=lambda loc: loc.order[2:],
        )
        for key, found in hits.items()
    }


def locator_text(title: str) -> str:
    """A section title, safe as the text of a Markdown link.

    Brackets are dropped rather than escaped. Appendix A calls its sections
    ``[audio]``, and both renderers stop a link's text at the first `]` -- so a
    bracketed title reaches the page as literal text with its URL showing. The
    brackets say nothing here that the link around them does not.
    """
    return title.replace("[", "").replace("]", "")


def build_index() -> list[str]:
    """Every name the program can utter, against the pages that discuss it.

    Not an appendix: it carries no `number`, which is what makes it render as a
    plain heading after Appendix J rather than as Appendix K.

    What is deliberately *not* here is the book's own section titles. They used
    to be entered wholesale, which put "Saving What a Run Changed" and "One
    Surface for the Whole Ensemble" in an index -- phrases nobody looks up, and
    each one a row pointing at the single section it was copied from. Topics
    belong to the contents page. An index holds terms, and the handful of plain
    words worth entering are curated in :data:`_CONCEPTS`.
    """
    paths = [p for p in bd.discover_chapters(REFERENCE_DIR) if p != INDEX_PATH]
    codes = code_terms()
    concepts = concept_terms(codes)
    found = scan(paths, codes, concepts)

    entries = sorted(
        (term for term in (*codes.values(), *concepts.values()) if term.key in found),
        # The qualifier before the key, so a run under one word reads bare
        # first and then by holder: `dither`, `dither (audio)`, `dither (color)`.
        key=lambda t: (t.sort, t.qualifier, t.key),
    )
    out = [
        "---",
        "generated: true",
        "---",
        "",
        "# Index",
        "",
        prose(
            f"Every name c64cast answers to — {len(entries)} of them — and the pages "
            "that discuss each one. A configuration key appears bare, and again under "
            "its section where two sections share the name; a parameter that belongs "
            "to a generator, an effect or a display mode is filed under its own name, "
            "with the holder in parentheses. A few entries are ordinary words rather "
            "than anything the program prints, for the reader who does not yet know "
            "what it calls the thing."
        ),
        "",
    ]
    for letter, group in _by_letter(entries):
        out += [f"## {letter}", ""]
        # Not :func:`identity`, which emboldens. An appendix bolds a name to
        # part it from the type and default stacked under it; an index entry is
        # one line and has nothing to be parted from, so bold said nothing --
        # and it said it in two faces at once. Jost Bold against Inconsolata
        # Bold at the same nominal size is a visibly heavier, wider letter, so
        # a column of `dither_strength` with "dithering" among them read as two
        # sizes of type. Set plain, the two faces sit together.
        rows = [
            [term.display, ", ".join(loc.markdown() for loc in found[term.key])] for term in group
        ]
        # `table: index` and not `table: fields`: same widths, and the locators
        # are additionally resolved to the pages they land on. See `_locators`
        # in scripts/build_book.py.
        out += fields_table("Term", rows, description="See", directive="table: index")
    return out


def _by_letter(entries: Sequence[Term]) -> list[tuple[str, list[Term]]]:
    """The entries grouped under their initial, in order.

    Anything that does not file under a letter opens the index under `#`, where
    a printed one has always put it. Nothing lands there today; a setting named
    for a number would go somewhere rather than vanish.
    """
    groups: dict[str, list[Term]] = {}
    for term in entries:
        initial = term.sort[:1].upper()
        groups.setdefault(initial if initial.isalpha() else "#", []).append(term)
    return sorted(groups.items(), key=lambda kv: (kv[0].isalpha(), kv[0]))


# ---------------------------------------------------------------------------
# The performance card's live-target table
# ---------------------------------------------------------------------------


def declared_by(target: introspect.LiveTargetDoc) -> str:
    """Who owns a live target, in the card's column.

    A target the running scene does not declare is a silent no-op: a knob on
    `source.ring_freq` moves nothing at all unless `moire2` is the generator on
    screen. So the question at the console is a membership test against the
    thing currently on screen, and a count — `14 generators` — cannot answer
    it. The names can, and they fit; the column wraps.

    The one shape a list answers badly is a target nearly everything declares,
    where the reader scans fourteen names to learn that the exception list is
    six. That inverts to `all but`, and only when the exceptions are a clear
    minority — an inversion the reader has to undo is worth a line saved, not
    a line broken even on.
    """
    # A holder of one — `scene` — would otherwise read `all`, which is true and
    # useless: the name is the thing the reader is matching against the screen.
    if len(target.owners) == 1:
        return code(target.owners[0])
    missing = [name for name in _holder_members(target.holder) if name not in target.owners]
    if not missing:
        return "all"
    if len(missing) * 2 < len(target.owners):
        return "all but " + ", ".join(code(name) for name in missing)
    return ", ".join(code(owner) for owner in target.owners)


@functools.cache
def _holder_members(holder: str) -> tuple[str, ...]:
    """Every class registered under a holder, in registry order, so
    :func:`declared_by` can name the ones a target leaves out — and tell a list
    that happens to be the whole registry from one that stops short of it."""
    return tuple(name for kind, name, _ in introspect._iter_live_holders() if kind == holder)


def card_live_targets() -> list[str]:
    """The card's most drift-prone page, generated in the same pass as Appendix F.

    Deliberately not the same table: a card is read at arm's length in a dark
    room, so it carries the target, its range and — compressed by
    :func:`declared_by` — who declares it.
    """
    targets = introspect.live_targets()
    out = [
        "---",
        "generated: true",
        "---",
        "",
        "# Live Targets",
        "",
        "*Every parameter a `param` mapping can name.*",
        "",
    ]
    for group in dict.fromkeys(t.group for t in targets):
        mine = [t for t in targets if t.group == group]
        out += [f"## {group}", ""]
        rows = []
        for t in mine:
            span = (
                scalar_range(t.lo, t.hi)
                if t.kind == "scalar" and t.lo is not None and t.hi is not None
                else code(f"{len(t.choices)} values")
            )
            rows.append([code(t.target.rpartition(".")[2]), span, declared_by(t)])
        # The holder heads the column rather than every cell under it. Appendix
        # F can afford a sentence saying it; a card cannot afford four, and the
        # column heading is where a prefix common to the whole column belongs.
        out += table([code(mine[0].holder + "."), "Range", "Declared by"], rows)
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
    INDEX_PATH: build_index,
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
