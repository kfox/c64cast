"""JSON Schema generator for the TOML config.

Consumes the same `introspect` model that drives `--describe`/`--list-*`, so
the editor schema can't drift from the code. Point a TOML-aware editor at the
committed `c64cast.schema.json` (via a `#:schema` directive at the top of a
config, supported by Taplo / the VS Code "Even Better TOML" extension) to get
key completion, enum completion, hover docs, and typo flagging while editing.

Run `c64cast --print-schema` to emit it; `tests/test_schema.py` asserts the
committed file matches a fresh build and that the example configs validate.
"""

from __future__ import annotations

from typing import Any

from . import introspect

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _split_union(type_str: str) -> list[str]:
    """Split a union annotation on top-level '|' only, leaving '|' inside
    brackets intact (so 'int | list[int | str]' -> ['int', 'list[int | str]'])."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(type_str):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append(type_str[start:i])
            start = i + 1
    parts.append(type_str[start:])
    return [p.strip() for p in parts if p.strip()]


def _json_type(type_str: str) -> dict[str, Any]:
    """Map a Python annotation string (e.g. 'str', 'int | None',
    'int | list[int | str]', 'list[str]') to the JSON-type portion of a schema."""
    parts = _split_union(type_str) if type_str else []
    json_types: list[str] = []
    for p in parts:
        if p == "None":
            json_types.append("null")
        elif p == "str":
            json_types.append("string")
        elif p == "bool":
            json_types.append("boolean")
        elif p == "int":
            json_types.append("integer")
        elif p == "float":
            json_types.append("number")
        elif p.startswith("list"):
            json_types.append("array")
        elif p.startswith("dict"):
            json_types.append("object")
        else:
            # Unknown annotation — leave unconstrained rather than wrong.
            return {}
    # De-dup while preserving order.
    seen: list[str] = []
    for t in json_types:
        if t not in seen:
            seen.append(t)
    if not seen:
        return {}
    return {"type": seen[0] if len(seen) == 1 else seen}


def _field_schema(
    name: str,
    type_str: str,
    *,
    help: str = "",
    choices: tuple[str, ...] = (),
    default: object = None,
    include_default: bool = True,
) -> dict[str, Any]:
    sch: dict[str, Any] = _json_type(type_str)
    if help:
        sch["description"] = help
    if choices:
        sch["enum"] = list(choices)
    if include_default and default is not None:
        sch["default"] = default
    return sch


def _section_schema(sd: introspect.SectionDoc) -> dict[str, Any]:
    props = {
        fd.name: _field_schema(
            fd.name, fd.type, help=fd.help, choices=fd.choices, default=fd.default
        )
        for fd in sd.fields
    }
    return {
        "type": "object",
        "description": sd.help,
        "additionalProperties": False,
        "properties": props,
    }


def _overlay_schema() -> dict[str, Any]:
    """Array-item schema for [[scenes.overlays]]: a base `type` enum plus one
    if/then per overlay exposing only that overlay's params (so editors offer
    the right keys and flag typos per overlay)."""
    docs = introspect.overlay_docs()
    all_of: list[dict[str, Any]] = []
    for od in docs:
        props: dict[str, Any] = {"type": {"const": od.name}}
        required = ["type"]
        for p in od.params:
            props[p.name] = _field_schema(
                p.name, p.type, help=p.help, default=p.default, include_default=not p.required
            )
            if p.required:
                required.append(p.name)
        all_of.append(
            {
                "if": {"properties": {"type": {"const": od.name}}, "required": ["type"]},
                "then": {
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {
                "description": "Overlay kind.",
                "enum": [od.name for od in docs],
            },
        },
        "allOf": all_of,
    }


def _scenes_schema() -> dict[str, Any]:
    """Array-item schema for [[scenes]]: all SceneCfg fields, plus per-type
    if/then that narrows the `display` enum to what each scene type supports."""
    # The full SceneCfg field set is the union across types (each type only
    # carries its applicable subset via `applies_to`).
    field_docs: dict[str, introspect.FieldDoc] = {}
    for sd in introspect.scene_types():
        for fd in sd.fields:
            field_docs.setdefault(fd.name, fd)

    props: dict[str, Any] = {}
    for fd in field_docs.values():
        if fd.name == "overlays":
            props["overlays"] = {
                "type": "array",
                "description": fd.help,
                "items": _overlay_schema(),
            }
        else:
            props[fd.name] = _field_schema(
                fd.name, fd.type, help=fd.help, choices=fd.choices, default=fd.default
            )

    all_of: list[dict[str, Any]] = []
    for sd in introspect.scene_types():
        if not sd.displays:
            continue
        all_of.append(
            {
                "if": {"properties": {"type": {"const": sd.name}}, "required": ["type"]},
                "then": {"properties": {"display": {"enum": list(sd.displays)}}},
            }
        )

    scene: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
    }
    if all_of:
        scene["allOf"] = all_of
    return scene


def build_schema() -> dict[str, Any]:
    """Build the full JSON Schema dict for a c64cast TOML config."""
    properties: dict[str, Any] = {
        sd.name: _section_schema(sd) for sd in introspect.config_sections()
    }
    properties["scenes"] = {
        "type": "array",
        "description": "Playlist scenes ([[scenes]] blocks).",
        "items": _scenes_schema(),
    }
    # [ensemble] is a master-only multi-system table; keep it permissive so
    # ensemble master TOMLs don't get flagged, without modeling its internals.
    properties["ensemble"] = {
        "type": "object",
        "description": "Multi-system ensemble definition (master TOML only).",
    }
    return {
        "$schema": SCHEMA_DIALECT,
        "title": "c64cast configuration",
        "description": "Generated by `c64cast --print-schema`. Do not edit by "
        "hand — regenerate with `make schema`.",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
