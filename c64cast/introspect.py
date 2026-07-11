"""Introspection layer — the single rendering surface over config metadata.

Everything an author needs to know to write a config (what sections/scenes/
overlays exist, what options each takes, valid values, defaults, and which
overlay works on which display mode) is already declared in code:

  * `config.py` dataclass fields carry ``metadata={"help", "choices",
    "applies_to"}``.
  * Overlay classes carry ``HELP`` / ``PARAM_HELP`` plus the restriction
    attributes (``REQUIRES_PETSCII`` / ``REQUIRES_AUDIO`` /
    ``COMPATIBLE_MODES``) and typed ``__init__`` signatures.
  * Display-mode classes carry ``is_bitmapped`` / ``is_petscii_compatible``.

This module reads all of that into one model (`config_sections`,
`scene_types`, `overlay_docs`, `display_modes`, `compat_matrix`) and renders
the terminal views (`render_list_*`, `render_describe`, `render_compat`). The
JSON-schema generator in `schema.py` consumes the same model, so the editor
schema, the `--describe` output, and the matrix can never disagree with the
code.

Kept deliberately import-light: it imports `config` (no numpy) and the overlay
registry, but NOT `modes` (which pulls in cv2/numpy) — the six display modes
are described by a small static table here, with `tests/test_introspect.py`
asserting that table stays in sync with the real `modes.py` classes.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, fields

from . import config as cfgmod
from . import overlays as ovmod

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDoc:
    name: str
    type: str
    default: object
    help: str
    choices: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    # On-C64 menu hint: "live" = the running scene can apply a change in place;
    # "rebuild" (default) = needs a scene rebuild, so the menu shows it read-only.
    # Internal — not emitted to schema/serializer/example.toml.
    apply: str = "rebuild"


@dataclass(frozen=True)
class SectionDoc:
    name: str  # TOML section name, e.g. "ultimate64"
    help: str
    fields: tuple[FieldDoc, ...]


@dataclass(frozen=True)
class ParamDoc:
    name: str
    type: str
    default: object  # `_REQUIRED` sentinel when no default
    required: bool
    help: str


@dataclass(frozen=True)
class OverlayDoc:
    name: str
    help: str
    params: tuple[ParamDoc, ...]
    requires_petscii: bool
    requires_audio: bool
    compatible_modes: tuple[str, ...]
    supports_bitmap_text: bool


@dataclass(frozen=True)
class ModeDoc:
    name: str  # config `display` value, e.g. "hires_edges"
    runtime_name: str  # DisplayMode.name (what COMPATIBLE_MODES matches)
    is_bitmapped: bool
    is_petscii_compatible: bool
    help: str
    is_bitmap_text_compatible: bool = False


@dataclass(frozen=True)
class SceneTypeDoc:
    name: str
    help: str
    displays: tuple[str, ...]  # supported `display` values ("" = N/A / fixed)
    fields: tuple[FieldDoc, ...]


class _Required:
    def __repr__(self) -> str:
        return "<required>"


_REQUIRED = _Required()


# ---------------------------------------------------------------------------
# Static descriptors (sync-tested against the runtime classes)
# ---------------------------------------------------------------------------

# TOML section name -> (dataclass, one-line section help). Mirrors the section
# list in config.load(); excludes [[scenes]] (see scene_types) and [ensemble].
_SECTIONS: tuple[tuple[str, type, str], ...] = (
    ("hardware", cfgmod.HardwareCfg, "Hardware backend selection."),
    ("teensyrom", cfgmod.TeensyromCfg, "TeensyROM+ backend connection."),
    ("ultimate64", cfgmod.Ultimate64Cfg, "Ultimate 64 target + transport."),
    ("video", cfgmod.VideoCfg, "Webcam input + experimental video paths."),
    ("audio", cfgmod.AudioCfg, "SID audio streaming."),
    ("vision", cfgmod.VisionCfg, "Webcam hand-gesture control (extra)."),
    ("interstitial", cfgmod.InterstitialCfg, "The 'UP NEXT' card shown between scenes."),
    ("playlist", cfgmod.PlaylistCfg, "Playlist behavior + video interleaving."),
    ("debug", cfgmod.DebugCfg, "Logging, heartbeat, profiling."),
    ("preview", cfgmod.PreviewCfg, "Local pygame mirror window (extra)."),
    ("recording", cfgmod.RecordingCfg, "Record the rendered display to a file."),
    (
        "color",
        cfgmod.ColorCfg,
        "Global pre-quantize color shaping for mcm/mhires/petscii: static channel boost + hue corrections, plus per-source adaptive auto_fit (video/slideshow).",
    ),
    (
        "dsp",
        cfgmod.DSPCfg,
        "Host-side audio DSP before the 4-bit DAC: compressor/limiter, expander (replaces the hard gate), pre-emphasis, and mic AGC.",
    ),
    ("control", cfgmod.ControlPlaneCfg, "HTTP control plane (extra)."),
    (
        "midi_control",
        cfgmod.MidiControlCfg,
        "MIDI CC control surface for live performance: scene jumps, style "
        "cycling, transport, live effect params (extra).",
    ),
    ("menu", cfgmod.MenuCfg, "On-C64 SPACE-key menu for live scene tweaks."),
    (
        "wled",
        cfgmod.WledCfg,
        "Broadcast WLED Audio Sync UDP from the playing SID so LAN LED matrices "
        "react to the music (WLED bridge Mode 3).",
    ),
)

# Display modes. `runtime_name` is DisplayMode.name (hires_edges and hires both
# build HiresDisplayMode whose name is "hires"). Sync-tested in tests.
_MODES: tuple[ModeDoc, ...] = (
    ModeDoc(
        "hires_edges",
        "hires",
        True,
        False,
        "320×200 bitmap, Canny edges (white on black). Default for live webcam.",
        is_bitmap_text_compatible=True,
    ),
    ModeDoc(
        "hires",
        "hires",
        True,
        False,
        "320×200 monochrome bitmap (luma-quantized per cell).",
        is_bitmap_text_compatible=True,
    ),
    ModeDoc(
        "mhires",
        "mhires",
        True,
        False,
        "160×200 4-color MCBM bitmap; per-cell palette (best for photos/video).",
        is_bitmap_text_compatible=True,
    ),
    ModeDoc("mcm", "mcm", False, False, "80×50 multicolor character mode (uploaded 2×2 charset)."),
    ModeDoc("petscii", "petscii", False, True, "40×25 PETSCII char mode (luma→glyph, hue→color)."),
    ModeDoc(
        "blank",
        "blank",
        False,
        True,
        "Solid char canvas with no video input — a base for overlays/title cards.",
    ),
)

# Scene type -> (help, supported `display` values). Mirrors validate_scene_cfg
# in config.py, which remains the authority. "" displays = the scene type fixes
# or ignores the display field.
_SCENE_TYPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "webcam",
        "Live webcam feed stylized through a display mode.",
        ("hires_edges", "hires", "mhires", "mcm", "petscii", "blank"),
    ),
    ("blank", "Empty canvas (no video) — a foundation for overlays.", ("blank", "hires_edges")),
    (
        "video",
        "Play a video file with synced audio until it ends.",
        ("mhires", "hires_edges", "hires", "mcm", "petscii", "blank"),
    ),
    ("waveform", "3-voice SID oscilloscope playing a .sid file (bitmap-only).", ()),
    ("midi", "Live MIDI input → SID synth + 3-voice oscilloscope (bitmap-only).", ()),
    (
        "asid",
        "Play an incoming ASID MIDI stream on the real SID + 3-voice oscilloscope (bitmap-only).",
        (),
    ),
    (
        "slideshow",
        "Cycle through still images, each stylized through a display mode.",
        ("mhires", "hires", "hires_edges", "mcm", "petscii", "random"),
    ),
    (
        "launcher",
        "Launch a native C64 program (.prg/.crt) and hand the "
        "machine over; idle timeout resets on player input.",
        (),
    ),
    (
        "generative",
        "Procedural video (plasma/tunnel/…) rendered to any display mode.",
        ("mhires", "hires", "hires_edges", "mcm", "petscii"),
    ),
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _field_docs(dc: type) -> list[FieldDoc]:
    """Build FieldDocs for a config dataclass, reading defaults off a fresh
    instance (so default_factory fields resolve to concrete values)."""
    blank = dc()
    out: list[FieldDoc] = []
    for f in fields(dc):
        md = f.metadata
        out.append(
            FieldDoc(
                name=f.name,
                type=str(f.type),
                default=getattr(blank, f.name),
                help=md.get("help", ""),
                choices=tuple(md.get("choices", ())),
                applies_to=tuple(md.get("applies_to", ())),
                apply=md.get("apply", "rebuild"),
            )
        )
    return out


def config_sections() -> list[SectionDoc]:
    return [
        SectionDoc(name=name, help=help_, fields=tuple(_field_docs(dc)))
        for name, dc, help_ in _SECTIONS
    ]


def display_modes() -> list[ModeDoc]:
    return list(_MODES)


def _scene_field_docs() -> list[FieldDoc]:
    return _field_docs(cfgmod.SceneCfg)


def scene_types() -> list[SceneTypeDoc]:
    """SceneCfg fields filtered per type via each field's `applies_to`
    metadata. A field with no `applies_to` applies to every scene type."""
    all_fields = _scene_field_docs()
    out: list[SceneTypeDoc] = []
    for name, help_, displays in _SCENE_TYPES:
        relevant = tuple(
            fd
            for fd in all_fields
            if fd.name == "type" or not fd.applies_to or name in fd.applies_to
        )
        out.append(SceneTypeDoc(name=name, help=help_, displays=displays, fields=relevant))
    return out


def scene_type_names() -> list[str]:
    return [name for name, _, _ in _SCENE_TYPES]


def _merged_param_help(cls: type) -> dict[str, str]:
    """Merge PARAM_HELP across the MRO so a subclass inherits shared
    parameter docs (e.g. CornerTextOverlay's corner/fg_color) and only needs
    to declare the params it adds. Most-derived wins."""
    merged: dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        ph = klass.__dict__.get("PARAM_HELP")
        if isinstance(ph, dict):
            merged.update(ph)
    return merged


def _overlay_params(cls: type) -> list[ParamDoc]:
    help_map = _merged_param_help(cls)
    sig = inspect.signature(cls.__init__)
    out: list[ParamDoc] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "audio") or p.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        has_default = p.default is not inspect.Parameter.empty
        # With `from __future__ import annotations`, the annotation is a string.
        ann = p.annotation
        type_str = (
            ann
            if isinstance(ann, str)
            else (getattr(ann, "__name__", str(ann)) if ann is not inspect.Parameter.empty else "")
        )
        out.append(
            ParamDoc(
                name=pname,
                type=type_str,
                default=p.default if has_default else _REQUIRED,
                required=not has_default,
                help=help_map.get(pname, ""),
            )
        )
    return out


def overlay_docs() -> list[OverlayDoc]:
    ovmod._load_all()
    out: list[OverlayDoc] = []
    for name in sorted(ovmod._REGISTRY):
        cls = ovmod._REGISTRY[name]
        out.append(
            OverlayDoc(
                name=name,
                help=getattr(cls, "HELP", ""),
                params=tuple(_overlay_params(cls)),
                requires_petscii=bool(getattr(cls, "REQUIRES_PETSCII", False)),
                requires_audio=bool(getattr(cls, "REQUIRES_AUDIO", False)),
                compatible_modes=tuple(getattr(cls, "COMPATIBLE_MODES", ())),
                supports_bitmap_text=bool(getattr(cls, "SUPPORTS_BITMAP_TEXT", False)),
            )
        )
    return out


def overlay_names() -> list[str]:
    ovmod._load_all()
    return sorted(ovmod._REGISTRY)


# ---------------------------------------------------------------------------
# Compatibility matrix (#3)
# ---------------------------------------------------------------------------


def overlay_mode_ok(ov: OverlayDoc, mode: ModeDoc) -> tuple[bool, str]:
    """Mirror overlays.validate_for_scene against a ModeDoc. Returns
    (ok, reason-when-not-ok)."""
    if ov.requires_petscii:
        petscii_ok = mode.is_petscii_compatible
        bitmap_ok = ov.supports_bitmap_text and mode.is_bitmap_text_compatible
        if not (petscii_ok or bitmap_ok):
            if ov.supports_bitmap_text:
                return False, "needs a text-capable mode (petscii/blank/hires/mhires)"
            return False, "needs PETSCII-compatible mode (petscii/blank)"
    if ov.compatible_modes and mode.runtime_name not in ov.compatible_modes:
        allowed = "/".join(ov.compatible_modes)
        return False, f"only on {allowed}"
    return True, ""


def compat_matrix() -> tuple[list[ModeDoc], list[tuple[OverlayDoc, list[bool]]]]:
    """Return (modes, rows) where each row is (overlay, [ok per mode])."""
    modes = display_modes()
    rows = [(ov, [overlay_mode_ok(ov, m)[0] for m in modes]) for ov in overlay_docs()]
    return modes, rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_default(val: object) -> str:
    if val is _REQUIRED:
        return "(required)"
    return repr(val)


def render_list_scenes() -> str:
    lines = ['Scene types (use `type = "<name>"` in a [[scenes]] block):', ""]
    for sd in scene_types():
        lines.append(f"  {sd.name:<11} {sd.help}")
    lines.append("")
    lines.append(
        "Run `--describe scene:<name>` for options, "
        "`--describe section:<name>` for a config section."
    )
    return "\n".join(lines)


def render_list_overlays() -> str:
    lines = ['Overlays (attach via [[scenes.overlays]] with `type = "<name>"`):', ""]
    for od in overlay_docs():
        flags = []
        if od.requires_petscii:
            flags.append("text+bitmap" if od.supports_bitmap_text else "petscii")
        if od.requires_audio:
            flags.append("audio")
        if od.compatible_modes:
            flags.append("modes=" + "/".join(od.compatible_modes))
        tag = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {od.name:<16} {od.help}{tag}")
    lines.append("")
    lines.append(
        "Run `--describe overlay:<name>` for options, `--compat` for the "
        "overlay × display-mode matrix."
    )
    return "\n".join(lines)


def render_list_modes() -> str:
    lines = ['Display modes (use `display = "<name>"`):', ""]
    for m in display_modes():
        kind = (
            "bitmap" if m.is_bitmapped else ("char/petscii" if m.is_petscii_compatible else "char")
        )
        lines.append(f"  {m.name:<12} ({kind:<12}) {m.help}")
    return "\n".join(lines)


def _render_fields(fds: list[FieldDoc] | tuple[FieldDoc, ...]) -> list[str]:
    lines: list[str] = []
    for fd in fds:
        lines.append(f"  {fd.name}  ({fd.type}, default {_fmt_default(fd.default)})")
        if fd.help:
            lines.append(f"      {fd.help}")
        if fd.choices:
            lines.append(f"      choices: {', '.join(fd.choices)}")
    return lines


def _render_section(sd: SectionDoc) -> str:
    lines = [f"[{sd.name}] — {sd.help}", ""]
    lines += _render_fields(sd.fields)
    return "\n".join(lines)


def _render_scene_type(sd: SceneTypeDoc) -> str:
    lines = [f"scene type {sd.name!r} — {sd.help}", ""]
    if sd.displays:
        lines.append(f"  supported display: {', '.join(sd.displays)}")
    else:
        lines.append("  display: fixed/ignored by this scene type")
    lines.append("")
    lines.append("  fields:")
    # Indent the shared field renderer one more level.
    for line in _render_fields(sd.fields):
        lines.append("  " + line if line else line)
    return "\n".join(lines)


def _render_overlay(od: OverlayDoc) -> str:
    lines = [f"overlay {od.name!r} — {od.help}", ""]
    restr = []
    if od.requires_petscii:
        if od.supports_bitmap_text:
            restr.append("text overlay: renders on petscii/blank and bitmap (hires/mhires)")
        else:
            restr.append("requires a PETSCII-compatible scene (petscii/blank)")
    if od.requires_audio:
        restr.append("requires [audio].enabled")
    if od.compatible_modes:
        restr.append("only on display modes: " + ", ".join(od.compatible_modes))
    if restr:
        for r in restr:
            lines.append(f"  ! {r}")
        lines.append("")
    lines.append("  options:")
    for p in od.params:
        lines.append(f"    {p.name}  ({p.type}, default {_fmt_default(p.default)})")
        if p.help:
            lines.append(f"        {p.help}")
    return "\n".join(lines)


def _render_mode(m: ModeDoc) -> str:
    kind = (
        "bitmap"
        if m.is_bitmapped
        else ("char (PETSCII-compatible)" if m.is_petscii_compatible else "char")
    )
    lines = [
        f"display mode {m.name!r} — {m.help}",
        "",
        f"  kind: {kind}",
        f"  PETSCII overlays: {'yes' if m.is_petscii_compatible else 'no'}",
        f"  bitmap text overlays: {'yes' if m.is_bitmap_text_compatible else 'no'}",
    ]
    return "\n".join(lines)


def render_describe(name: str) -> str:
    """Resolve `name` (optionally prefixed `section:` / `scene:` / `overlay:` /
    `mode:`) to one entity and render it. Lists candidates on ambiguity and a
    helpful error on no match."""
    kind, _, bare = name.partition(":") if ":" in name else ("", "", name)
    bare = bare.strip()

    sections = {s.name: s for s in config_sections()}
    scenes = {s.name: s for s in scene_types()}
    overlays_ = {o.name: o for o in overlay_docs()}
    modes = {m.name: m for m in display_modes()}

    if kind:
        table = {
            "section": (sections, _render_section),
            "scene": (scenes, _render_scene_type),
            "overlay": (overlays_, _render_overlay),
            "mode": (modes, _render_mode),
        }.get(kind)
        if table is None:
            return f"unknown describe prefix {kind!r} (use section:, scene:, overlay:, or mode:)"
        registry, renderer = table
        ent = registry.get(bare)
        if ent is None:
            avail = ", ".join(sorted(registry))
            return f"unknown {kind} {bare!r}. Available: {avail}"
        return renderer(ent)  # type: ignore[no-any-return]

    # Unprefixed: collect matches across all kinds.
    matches: list[tuple[str, object, object]] = []
    if bare in sections:
        matches.append(("section", sections[bare], _render_section))
    if bare in scenes:
        matches.append(("scene", scenes[bare], _render_scene_type))
    if bare in overlays_:
        matches.append(("overlay", overlays_[bare], _render_overlay))
    if bare in modes:
        matches.append(("mode", modes[bare], _render_mode))

    if not matches:
        return f"nothing named {bare!r}. Try --list-scenes, --list-overlays, or --list-modes."
    if len(matches) > 1:
        kinds = ", ".join(f"{k}:{bare}" for k, _, _ in matches)
        return (
            f"{bare!r} is ambiguous — matches {len(matches)} kinds. "
            f"Disambiguate with one of: {kinds}"
        )
    _, ent, renderer = matches[0]
    return renderer(ent)  # type: ignore[operator]


def render_compat() -> str:
    """Render the overlay × display-mode compatibility matrix. A ✓ means the
    overlay attaches; a ·  is a gap. PETSCII-only overlays show up as a wall
    of gaps in the bitmap columns — that block is the parity worklist."""
    modes, rows = compat_matrix()
    name_w = max((len(ov.name) for ov, _ in rows), default=8)
    # Column headers: abbreviate to keep the grid narrow.
    abbr = {
        "hires_edges": "h.edg",
        "hires": "hires",
        "mhires": "mhire",
        "mcm": "mcm",
        "petscii": "petsc",
        "blank": "blank",
    }
    col_w = 6
    header = " " * (name_w + 4) + "".join(f"{abbr.get(m.name, m.name):<{col_w}}" for m in modes)
    lines = ["Overlay × display-mode compatibility (✓ = works, · = unsupported):", "", header]
    for ov, oks in rows:
        cells = "".join(f"{'✓' if ok else '·':<{col_w}}" for ok in oks)
        lines.append(f"  {ov.name:<{name_w + 2}}{cells}")
    lines.append("")
    lines.append("Columns: " + ", ".join(f"{abbr.get(m.name, m.name)}={m.name}" for m in modes))
    lines.append("Note: audio overlays additionally need [audio].enabled.")
    return "\n".join(lines)
