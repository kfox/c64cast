"""Serialize a :class:`config.Config` back to TOML — the inverse of
``config.load``.

This is the third renderer over the single-source-of-truth config metadata
(``introspect.py`` renders ``--describe`` / ``--list-*`` / ``--compat``;
``schema.py`` renders the JSON schema). It reuses the same ``introspect``
model so the per-field help comments here can't drift from ``--describe``, and
the section / scene-field ordering matches the discovery commands.

Contract: ``load(dumps(cfg)) == cfg`` for any Config produced by ``load`` (the
round-trip property test in ``tests/test_config_serialize.py`` enforces this
across every shipped example config). It's the foundation both deferred config
UX surfaces need — the wizard writes its result through here, and a future
"dump the current live state to config" action serializes the running Config.

**What ``minimal`` measures against is the caller's to say** (``baseline``).
Omitted, it is the dataclass defaults, which is right only when the Config was
built on nothing else. Every caller that serializes a Config the *loader*
produced passes ``config.machine_baseline()`` instead, because such a Config
already carries the machine-settings layer: measured against the dataclass
defaults, a machine setting that differs from a shipped default gets written
into the file it was only ever layered under — pinning this machine's capture
device or connection URL into a show config that is then copied to another
machine, or overriding it there forever. The rule is one line: **the baseline
must be the Config the serialized one was built on top of.** Handing in a
baseline the Config was *not* built on is the mirror-image mistake — a blank
Config dumped against a machine baseline writes every dataclass default the
machine layer disagrees with, which is the same bug pointing the other way.

Hand-rolled rather than via a TOML-writer dependency: the value space is small
and fully controlled (the dataclass field types), comments aren't representable
by ``tomli-w``, and ``tomlkit`` would be a new runtime dep for output the
round-trip test already proves correct. The escaper below covers basic strings;
the test suite exercises it against the real configs.

Scope (v1): a single standalone/per-system Config. Ensemble *master* TOMLs
(``[ensemble]`` + ``systems``) are rejected — they're authored across multiple
files and aren't what the wizard produces.
"""

from __future__ import annotations

import logging
import math
import os
import re

from c64cast import __version__

from . import config as cfgmod
from . import introspect, paths

log = logging.getLogger(__name__)

_PUBLISHED_SCHEMA_URL = (
    "https://raw.githubusercontent.com/kfox/c64cast/{ref}/c64cast/data/c64cast.schema.json"
)

# The template above read the other way; spelled out rather than escaped from
# it, with `test_a_published_url_reads_back` pinning the two spellings together.
_PINNED_URL_RE = re.compile(
    r"https://raw\.githubusercontent\.com/kfox/c64cast/v(?P<version>[^/]+)"
    r"/c64cast/data/c64cast\.schema\.json\Z"
)


def _published_schema_url(version: str) -> str:
    """URL of the schema as published for `version`, or on `main` if unreleased."""
    ref = f"v{version}" if version and version[0].isdigit() else "main"
    return _PUBLISHED_SCHEMA_URL.format(ref=ref)


# Fallback for the `#:schema` first line, used only when the packaged schema is
# not on disk to point at. Pinned to *this* version rather than a moving ref: a
# schema newer than the program offers keys this install will reject. The pin is
# also why it is the fallback — `schema_directive_for` prefers the installed
# copy, which every upgrade rewrites.
DEFAULT_SCHEMA_PATH = _published_schema_url(__version__)

# Fields never written to disk and never echoed back as an ordinary field value:
# each grants remote control of the host. Public, because `config_store`
# withholds the same set from the web console's form data — one list, so a secret
# cannot be safe in the file and visible in the browser.
#
# It governs `describe()`'s form, `_editable_fields()` and `dumps()`. It does NOT
# reach `config_store.read()`'s raw `text`, which still carries these verbatim
# (`web_api.api_config_read` is what gates that text behind the full token), and
# `save_machine_settings` deliberately re-emits them, since the machine-settings
# file is the one file they live in.
SECRET_FIELDS = frozenset(
    {
        ("ultimate64", "dma_password"),
        ("web", "token"),
        ("web", "token_file"),
        ("web", "viewer_token"),
        ("control", "token"),
        ("control", "viewer_token"),
    }
)

# List-of-table fields, rendered as [[parent.child]] blocks after the parent's
# scalar keys because TOML forbids a scalar key once a sub-table header opens.
_COLOR_TABLE_ARRAY = "hue_corrections"  # under [color]
_SCENE_TABLE_ARRAY = "overlays"  # under [[scenes]]
_PERF_TABLE_ARRAY = "clips"  # under [performance]

_MIDI_TABLE_ARRAY = "cc_map"  # under [midi_control]

# Section -> (field, [[header]]). One lookup `_emit_section` consults twice — to
# skip the field in the scalar loop, then to route its rows.
_SECTION_TABLE_ARRAYS: dict[str, tuple[str, str]] = {
    "color": (_COLOR_TABLE_ARRAY, "color.hue_corrections"),
    "performance": (_PERF_TABLE_ARRAY, "performance.clips"),
    "midi_control": (_MIDI_TABLE_ARRAY, "midi_control.cc_map"),
}

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_STR_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class SerializeError(Exception):
    """Raised when a Config can't be represented as TOML (e.g. an ensemble
    master, or a non-finite float). Message is end-user readable."""


def _fmt_str(s: str) -> str:
    out = []
    for ch in s:
        esc = _STR_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _fmt_key(k: str) -> str:
    return k if _BARE_KEY_RE.match(k) else _fmt_str(k)


def _fmt_value(v: object) -> str:
    """Format a scalar / list-of-scalars / flat dict as a TOML value.

    Nested list-of-tables (overlays, hue_corrections) never reach here — the
    emitters route those to [[…]] blocks. A dict here is a flat string→string
    map (e.g. waveform_colors) rendered as an inline table."""
    if isinstance(v, bool):  # before int — bool is an int subclass
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise SerializeError(f"cannot serialize non-finite float {v!r} to TOML")
        return repr(v)
    if isinstance(v, str):
        return _fmt_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ", ".join(f"{_fmt_key(str(k))} = {_fmt_value(val)}" for k, val in v.items())
        return "{ " + inner + " }"
    raise SerializeError(f"cannot serialize value of type {type(v).__name__}: {v!r}")


def _should_emit(value: object, default: object, *, minimal: bool) -> bool:
    """A field is written when it carries information: never None (TOML can't
    represent it, and None always means "fall back to the dataclass default"),
    and — in minimal mode — only when it differs from that default."""
    if value is None:
        return False
    return not (minimal and value == default)


def _comment_lines(help_text: str, choices: tuple[str, ...], indent: str) -> list[str]:
    if not help_text and not choices:
        return []
    text = help_text
    if choices:
        suffix = "choices: " + ", ".join(choices)
        text = f"{text} ({suffix})" if text else suffix
    return [f"{indent}# {text}"]


def _table_rows(
    section: object, base: object, attr: str, *, minimal: bool
) -> list[dict[str, object]]:
    """A section's list-of-tables field, dropped when the baseline already
    carries it. A list is written whole or not at all — TOML has no way to say
    "these rows on top of those" — so the only honest minimal answer is to omit
    it when this layer added nothing to it."""
    rows = list(getattr(section, attr) or [])
    if minimal and base is not None and rows == list(getattr(base, attr) or []):
        return []
    return rows


def _emit_table_array(header: str, rows: list[dict[str, object]]) -> list[str]:
    """Render a list of plain dicts as repeated [[header]] blocks (used for
    [[color.hue_corrections]] and [[scenes.overlays]]). `type` floats to the
    top of an overlay block for readability; otherwise insertion order."""
    lines: list[str] = []
    for row in rows:
        lines.append(f"[[{header}]]")
        keys = list(row)
        if "type" in row:
            keys = ["type"] + [k for k in keys if k != "type"]
        for k in keys:
            lines.append(f"{_fmt_key(k)} = {_fmt_value(row[k])}")
        lines.append("")
    return lines


def _emit_section(
    cfg: cfgmod.Config,
    sd: introspect.SectionDoc,
    *,
    annotate: bool,
    minimal: bool,
    baseline: cfgmod.Config | None = None,
    include_secrets: bool = False,
) -> list[str]:
    section = getattr(cfg, sd.name)
    base = getattr(baseline, sd.name) if baseline is not None else None
    table_array = _SECTION_TABLE_ARRAYS.get(sd.name)
    body: list[str] = []
    for fd in sd.fields:
        if not include_secrets and (sd.name, fd.name) in SECRET_FIELDS:
            continue
        if table_array is not None and fd.name == table_array[0]:
            continue  # emitted as a [[...]] block below
        value = getattr(section, fd.name)
        default = fd.default if base is None else getattr(base, fd.name)
        if not _should_emit(value, default, minimal=minimal):
            continue
        if annotate:
            body += _comment_lines(fd.help, fd.choices, "")
        body.append(f"{_fmt_key(fd.name)} = {_fmt_value(value)}")

    # After the section's scalar keys — see `_COLOR_TABLE_ARRAY`.
    table_rows: list[dict[str, object]] = []
    table_header = ""
    if table_array is not None:
        table_rows = _table_rows(section, base, table_array[0], minimal=minimal)
        table_header = table_array[1]

    if not body and not table_rows:
        return []  # nothing set in this section — skip the header entirely

    lines: list[str] = []
    if annotate and sd.help:
        lines.append(f"# {sd.help}")
    lines.append(f"[{sd.name}]")
    lines += body
    lines.append("")
    if table_rows:
        lines += _emit_table_array(table_header, table_rows)
    return lines


def _emit_scene(
    s: cfgmod.SceneCfg,
    field_docs: dict[str, tuple[introspect.FieldDoc, ...]],
    all_fields: tuple[introspect.FieldDoc, ...],
    *,
    annotate: bool,
    minimal: bool,
) -> list[str]:
    # `introspect` has already done the `applies_to` filtering.
    fields = field_docs.get(s.type, all_fields)
    # A field this type doesn't claim but that carries a non-default value anyway
    # still has to round-trip: `load` never enforces `applies_to`, so dropping it
    # here would silently rewrite the file out from under its author.
    leftover = tuple(fd for fd in all_fields if fd.name not in {f.name for f in fields})
    lines = ["[[scenes]]"]
    # The discriminator: written even at its default, so the block is unambiguous.
    lines.append(f"type = {_fmt_value(s.type)}")
    for fd in (*fields, *leftover):
        if fd.name in ("type", _SCENE_TABLE_ARRAY, "color"):
            continue
        value = getattr(s, fd.name)
        if not _should_emit(value, fd.default, minimal=minimal):
            continue
        if annotate:
            lines += _comment_lines(fd.help, fd.choices, "")
        lines.append(f"{_fmt_key(fd.name)} = {_fmt_value(value)}")
    lines.append("")
    if s.color:
        lines += _emit_scene_color(s.color, annotate=annotate)
    if s.overlays:
        lines += _emit_table_array("scenes.overlays", list(s.overlays))
    return lines


def _emit_scene_color(color: dict[str, object], *, annotate: bool) -> list[str]:
    """Render a scene's ``[scenes.color]`` override: the raw authored keys
    (scene_cfg.color IS the sparse dict, so every key present is authored —
    there's no default to measure against), with ``hue_corrections`` routed
    to its own ``[[scenes.color.hue_corrections]]`` blocks after the scalar
    keys, the same ordering constraint [[scenes.overlays]] follows."""
    color_docs = {
        fd.name: fd for sd in introspect.config_sections() if sd.name == "color" for fd in sd.fields
    }
    hue_corrections = color.get(_COLOR_TABLE_ARRAY)
    lines = ["[scenes.color]"]
    for k, v in color.items():
        if k == _COLOR_TABLE_ARRAY:
            # `color` is the scene's sparse dict, so an empty override is still
            # an authored key and has to round-trip as one.
            if isinstance(v, list) and not v:
                lines.append(f"{_fmt_key(k)} = []")
            continue
        if annotate:
            fd = color_docs.get(k)
            if fd is not None:
                lines += _comment_lines(fd.help, fd.choices, "")
        lines.append(f"{_fmt_key(k)} = {_fmt_value(v)}")
    lines.append("")
    if isinstance(hue_corrections, list) and hue_corrections:
        rows: list[dict[str, object]] = [dict(hc) for hc in hue_corrections]
        lines += _emit_table_array("scenes.color.hue_corrections", rows)
    return lines


def schema_directive_for(out_path: str) -> str:
    """The value that belongs after ``#:schema`` on line 1 of the config at
    ``out_path`` — what gives a TOML-aware editor key/value completion.

    Names the schema that ships *inside this install*
    (:func:`paths.packaged_schema_path`) rather than a published URL, which is
    what makes the line survive upgrades: an upgrade rewrites that file in
    place, so a config pointing at it is checked against the c64cast actually
    running, release after release, with nothing for the reader to maintain.

    Relative when the schema sits *inside* the config's own directory tree — a
    source checkout, or a project-local ``.venv`` — because that survives moving
    the whole tree. Absolute as soon as it would take a single ``..``: a user- or
    system-level install turns the relative form into an unreadable climb out to
    ``site-packages`` that also breaks the moment the config moves. Falls back to
    :data:`DEFAULT_SCHEMA_PATH` (the version-pinned URL) if the schema somehow
    isn't on disk.

    Lives here rather than in ``wizard`` (where it started, when ``--init`` was
    its only caller) because line 1 is this module's to write, and ``doctor``
    can't reach for the config *builder* just to check a config it loaded."""
    schema = paths.packaged_schema_path()
    if not schema.is_file():
        return DEFAULT_SCHEMA_PATH
    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        rel = os.path.relpath(schema, out_dir)
    except ValueError:
        # Windows only: `relpath` raises across drives, there being no relative
        # path at all. Same answer as the "would have to climb" case below.
        return str(schema)
    if rel.startswith(".."):
        return str(schema)
    return rel if rel.startswith(os.sep) else f".{os.sep}{rel}"


def pinned_url_version(directive: str) -> str | None:
    """The c64cast version a ``#:schema`` value pins, for one of our own
    published URLs; None for anything else — a local path, a fork's URL, a
    team's hand-picked schema.

    A pinned URL is the one directive form an upgrade leaves behind, since it
    names a snapshot instead of the install. Reading the version back out is how
    ``--doctor`` can say so (see ``doctor._validate_schema_directive``)."""
    m = _PINNED_URL_RE.match(directive)
    return m.group("version") if m else None


def dumps(
    cfg: cfgmod.Config,
    *,
    annotate: bool = True,
    minimal: bool = True,
    schema_path: str | None = DEFAULT_SCHEMA_PATH,
    baseline: cfgmod.Config | None = None,
    include_secrets: bool = False,
) -> str:
    """Serialize `cfg` to a TOML string.

    annotate    — prepend the schema directive + per-section/-field help
                  comments (the authored-config style). False = bare values.
    minimal     — omit fields equal to the baseline (the way a human writes a
                  config). False = write every set field.
    schema_path — value for the leading ``#:schema`` directive; None omits it.
    baseline    — what `minimal` measures a field against. None = the dataclass
                  defaults; pass `config.machine_baseline()` for any Config the
                  loader produced, so a value inherited from the machine-settings
                  layer isn't written into the file that inherited it. It must be
                  the Config this one was built on top of — see the module
                  docstring.
    include_secrets — emit the `SECRET_FIELDS` values too. **One sanctioned
                  caller**, :func:`save_machine_settings`, and its docstring
                  says why; anything else writing a config must leave this
                  alone, because the file it produces may be committed, served
                  or echoed.

    Scene fields always measure against the dataclass defaults: machine settings
    hold cross-run defaults, never playlists, so there is no scene layer under a
    scene. The DMA password is never emitted (see `SECRET_FIELDS`). Raises
    `SerializeError` for ensemble masters or non-finite floats."""
    if cfg.ensemble is not None:
        raise SerializeError(
            "ensemble master configs aren't serializable yet — dump each "
            "per-system Config separately, or hand-author the [ensemble] "
            "master."
        )

    lines: list[str] = []
    if schema_path:
        lines.append(f"#:schema {schema_path}")
        lines.append("")

    for sd in introspect.config_sections():
        lines += _emit_section(
            cfg,
            sd,
            annotate=annotate,
            minimal=minimal,
            baseline=baseline,
            include_secrets=include_secrets,
        )

    if cfg.scenes:
        field_docs = {st.name: st.fields for st in introspect.scene_types()}
        all_fields = tuple(field_docs.get("webcam", ()))
        # Union of every type's fields as the unknown-type fallback.
        seen = {fd.name for fd in all_fields}
        for st_fields in field_docs.values():
            for fd in st_fields:
                if fd.name not in seen:
                    all_fields += (fd,)
                    seen.add(fd.name)
        for s in cfg.scenes:
            lines += _emit_scene(s, field_docs, all_fields, annotate=annotate, minimal=minimal)

    text = "\n".join(lines).rstrip("\n")
    return text + "\n"


def _secrets_carried(cfg: cfgmod.Config) -> tuple[str, ...]:
    """The ``SECRET_FIELDS`` `cfg` actually holds a value for, named the way a
    config file spells them, sorted."""
    blank = cfgmod.Config()
    return tuple(
        f"[{section}].{name}"
        for section, name in sorted(SECRET_FIELDS)
        if getattr(getattr(cfg, section), name) != getattr(getattr(blank, section), name)
    )


def save_machine_settings(cfg: cfgmod.Config) -> tuple[str, str, tuple[str, ...]]:
    """Write the machine-settings layer for `cfg`. ``(path, echo_text, kept)``.

    **The one writer for that file.** `--save-settings`
    (:func:`c64cast.app.cli_commands.run_save_settings`) and the appliance
    setup form (:mod:`c64cast.control.setup_api`) are both merge-and-rewrite
    paths: they seed a Config from :func:`config.apply_machine_settings`,
    overlay what this invocation asked for, and write the whole file back. That
    only works if the rewrite carries what the seed read, and `dumps` suppresses
    every :data:`SECRET_FIELDS` value — so each of those callers used to rewrite
    the file *without* the ``dma_password`` or ``[web] token`` an operator had
    put there by hand. The setup form's did it silently, which on an appliance
    means a box that can no longer talk to its own password-protected U64 and a
    ``[web] token`` pin erased by an unauthenticated caller. Preserving them can
    only be a guarantee if there is one writer, which is this function; the
    docstring that claimed the two paths matched "exactly" is what a shared
    function is for.

    No ``baseline``: this *is* the machine layer, so the dataclass defaults are
    what it sits on. Measuring it against itself would write an empty file.

    ``echo_text`` is the same content **without** the secrets, because
    ``--save-settings`` prints what it saved to stdout and a terminal is not
    where a credential belongs; ``kept`` names the keys that were preserved so
    a caller can say so without quoting a value. A file carrying one is
    restricted to ``0600`` explicitly — ``atomic_write_text`` already lands at
    that mode through ``mkstemp``, and stating it here makes it this file's
    guarantee rather than a helper's implementation detail, the way
    ``setup_api._write_token`` and ``serve._persist_viewer_token`` do."""
    from c64cast.control.transport import atomic_write_text

    dest = paths.settings_path()
    kept = _secrets_carried(cfg)
    echo_text = dumps(cfg, minimal=True, schema_path=None)
    on_disk = (
        dumps(cfg, minimal=True, schema_path=None, include_secrets=True) if kept else echo_text
    )
    atomic_write_text(dest, on_disk)
    if kept:
        try:
            dest.chmod(0o600)
        except OSError:
            log.warning("could not restrict permissions on %s", dest)
    return str(dest), echo_text, kept


def dump(cfg: cfgmod.Config, path: str, **kwargs: object) -> None:
    """Serialize `cfg` and write it to `path` (UTF-8). kwargs pass through to
    `dumps` (annotate / minimal / schema_path / baseline)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(cfg, **kwargs))  # type: ignore[arg-type]
