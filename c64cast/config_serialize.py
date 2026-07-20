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

import math
import re

from . import config as cfgmod
from . import introspect

# Default value for the editor-schema directive written as the first line, so a
# serialized config gets Taplo / Even-Better-TOML autocomplete just like the
# shipped examples. Relative to the file's own location; callers writing into a
# subdirectory pass their own (e.g. "../../c64cast.schema.json").
DEFAULT_SCHEMA_PATH = "./c64cast.schema.json"

# Never written to disk — it's a secret, supplied via the C64CAST_DMA_PASSWORD
# env var or hand-added to a non-committed file (see docs/usage.md). Omitting it
# keeps the serializer safe to point at a checked-in path.
_SECRET_FIELDS = frozenset({("ultimate64", "dma_password")})

# List-of-table fields that must render as [[parent.child]] blocks AFTER the
# parent's scalar keys (TOML forbids scalar keys after a sub-table header is
# opened). Handled out-of-band by the section/scene emitters below.
_COLOR_TABLE_ARRAY = "hue_corrections"  # under [color]
_SCENE_TABLE_ARRAY = "overlays"  # under [[scenes]]
_PERF_TABLE_ARRAY = "clips"  # under [performance]

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


# ---------------------------------------------------------------------------
# Scalar formatting
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Field selection
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Section + scene emitters
# ---------------------------------------------------------------------------


def _emit_table_array(header: str, rows: list[dict[str, object]], annotate: bool) -> list[str]:
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
    cfg: cfgmod.Config, sd: introspect.SectionDoc, *, annotate: bool, minimal: bool
) -> list[str]:
    section = getattr(cfg, sd.name)
    body: list[str] = []
    for fd in sd.fields:
        if (sd.name, fd.name) in _SECRET_FIELDS:
            continue
        if sd.name == "color" and fd.name == _COLOR_TABLE_ARRAY:
            continue  # emitted as [[color.hue_corrections]] below
        if sd.name == "performance" and fd.name == _PERF_TABLE_ARRAY:
            continue  # emitted as [[performance.clips]] below
        value = getattr(section, fd.name)
        if not _should_emit(value, fd.default, minimal=minimal):
            continue
        if annotate:
            body += _comment_lines(fd.help, fd.choices, "")
        body.append(f"{_fmt_key(fd.name)} = {_fmt_value(value)}")

    # Trailing list-of-tables, emitted after the section's scalar keys (TOML
    # forbids scalar keys once a sub-table header opens).
    table_rows: list[dict[str, object]] = []
    table_header = ""
    if sd.name == "color":
        table_rows = list(getattr(section, _COLOR_TABLE_ARRAY) or [])
        table_header = "color.hue_corrections"
    elif sd.name == "performance":
        table_rows = list(getattr(section, _PERF_TABLE_ARRAY) or [])
        table_header = "performance.clips"

    if not body and not table_rows:
        return []  # nothing set in this section — skip the header entirely

    lines: list[str] = []
    if annotate and sd.help:
        lines.append(f"# {sd.help}")
    lines.append(f"[{sd.name}]")
    lines += body
    lines.append("")
    if table_rows:
        lines += _emit_table_array(table_header, table_rows, annotate)
    return lines


def _emit_scene(
    s: cfgmod.SceneCfg,
    field_docs: dict[str, tuple[introspect.FieldDoc, ...]],
    all_fields: tuple[introspect.FieldDoc, ...],
    *,
    annotate: bool,
    minimal: bool,
) -> list[str]:
    # Only the fields that apply to this scene's type (introspect already did
    # the applies_to filtering); fall back to every field for an unknown type.
    fields = field_docs.get(s.type, all_fields)
    lines = ["[[scenes]]"]
    # `type` is the discriminator — always written, even when it's the default,
    # so the block is unambiguous and copy-pasteable.
    lines.append(f"type = {_fmt_value(s.type)}")
    for fd in fields:
        if fd.name == "type" or fd.name == _SCENE_TABLE_ARRAY:
            continue
        value = getattr(s, fd.name)
        if not _should_emit(value, fd.default, minimal=minimal):
            continue
        if annotate:
            lines += _comment_lines(fd.help, fd.choices, "")
        lines.append(f"{_fmt_key(fd.name)} = {_fmt_value(value)}")
    lines.append("")
    if s.overlays:
        lines += _emit_table_array("scenes.overlays", list(s.overlays), annotate)
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def dumps(
    cfg: cfgmod.Config,
    *,
    annotate: bool = True,
    minimal: bool = True,
    schema_path: str | None = DEFAULT_SCHEMA_PATH,
) -> str:
    """Serialize `cfg` to a TOML string.

    annotate    — prepend the schema directive + per-section/-field help
                  comments (the authored-config style). False = bare values.
    minimal     — omit fields equal to their dataclass default (the way a
                  human writes a config). False = write every set field.
    schema_path — value for the leading ``#:schema`` directive; None omits it.

    The DMA password is never emitted (see `_SECRET_FIELDS`). Raises
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
        lines += _emit_section(cfg, sd, annotate=annotate, minimal=minimal)

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

    # Collapse the trailing blank line; guarantee a single terminating newline.
    text = "\n".join(lines).rstrip("\n")
    return text + "\n"


def dump(cfg: cfgmod.Config, path: str, **kwargs: object) -> None:
    """Serialize `cfg` and write it to `path` (UTF-8). kwargs pass through to
    `dumps` (annotate / minimal / schema_path)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(cfg, **kwargs))  # type: ignore[arg-type]
