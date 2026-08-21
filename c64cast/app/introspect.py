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
import re
import textwrap
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from c64cast.scenes import overlays as ovmod

from . import config as cfgmod
from . import paths as pathsmod

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
    # The named set a field's *string* values are drawn from, when they come
    # from one small enough to offer whole: "c64color" is the sixteen palette
    # entries by name. `choices` can't say this, because these fields accept an
    # index as well and a picker would refuse it. Empty = free text.
    vocabulary: str = ""


@dataclass(frozen=True)
class SectionDoc:
    name: str  # TOML section name, e.g. "ultimate64"
    help: str
    fields: tuple[FieldDoc, ...]
    #: Whether a running session's *reload* picks this section up, or it takes a
    #: restart. `FieldDoc.apply` answers the narrower question of whether a
    #: change lands without even a scene rebuild; this is the one a console has
    #: to answer at the moment somebody saves.
    reload: bool = False


@dataclass(frozen=True)
class ParamDoc:
    name: str
    type: str
    default: object  # `REQUIRED` sentinel when no default
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
    # Which media_store.py kind(s) this type's `file =` field browses. A
    # field's own `vocabulary` ("media") can't say this by itself — the same
    # field means videos on a video scene and .sid files on a waveform one —
    # so it rides on the scene type instead. Empty for a type with no `file =`.
    media_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveTargetDoc:
    """One live-tunable ``param`` target (a knob/pad binding for
    ``[midi_control].cc_map`` and the ``--midi-setup`` wizard's target picker).

    The single source of truth over the ``LIVE_PARAMS`` / ``LIVE_CHOICES`` class
    attributes scattered across ``effects`` / ``generators`` / ``voice_scope`` /
    ``modes`` — :func:`live_targets` collects them so the wizard and a drift test
    can't fall behind the registries."""

    target: str  # the cc_map "target" string, e.g. "mode.dither_strength"
    holder: str  # "mode" | "effect" | "source" | "scene"
    group: str  # picker section: "Color pipeline" | "Effect" | "Generator" | "Scope"
    kind: str  # "scalar" (LIVE_PARAMS) | "choice" (LIVE_CHOICES)
    lo: float | None = None  # scalar range low (informational; runtime uses the live holder's)
    hi: float | None = None  # scalar range high
    choices: tuple[str, ...] = ()  # choice values
    owners: tuple[str, ...] = ()  # which registered classes declare it (for display)
    # The named set a choice's values are drawn from, mirroring FieldDoc's own
    # `vocabulary` — "c64color" is what tells a console to render swatches
    # instead of a <select>. "" for every scalar and most choices.
    vocabulary: str = ""


class _Required:
    def __repr__(self) -> str:
        return "<required>"


REQUIRED = _Required()


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
    ("preview", cfgmod.PreviewCfg, "Local mirror window of the C64 display."),
    ("recording", cfgmod.RecordingCfg, "Record the rendered display to a file."),
    (
        "color",
        cfgmod.ColorCfg,
        "Global pre-quantize color shaping for mcm/mhires/petscii: static channel boost + hue corrections, plus per-source adaptive auto_fit (video/slideshow). Any field here is a show-wide default a scene's own [scenes.color] table may override — see `scene:` field `color` under --describe.",
    ),
    (
        "dsp",
        cfgmod.DSPCfg,
        "Host-side audio DSP before the 4-bit DAC: compressor/limiter, expander (replaces the hard gate), pre-emphasis, and mic AGC.",
    ),
    (
        "audio_features",
        cfgmod.AudioFeaturesCfg,
        "Analyzer that turns live audio input into reactive-visual features "
        "(level / bands / transients / tempo) for a generative scene with "
        "audio_source = 'mic' and reactive = true.",
    ),
    ("control", cfgmod.ControlPlaneCfg, "HTTP control plane (extra)."),
    (
        "web",
        cfgmod.WebCfg,
        "Web console host (--serve): a long-lived server that owns the hardware "
        "and starts/stops sessions on request (extra).",
    ),
    (
        "midi_control",
        cfgmod.MidiControlCfg,
        "MIDI CC control surface for live performance: scene jumps, style "
        "cycling, transport, live effect params (extra).",
    ),
    (
        "performance",
        cfgmod.PerformanceCfg,
        "Live-performance tempo/beat grid: follow an external MIDI clock or "
        "free-run at a static/tapped BPM (drives launch quantization + "
        "tempo-locked effects).",
    ),
    ("menu", cfgmod.MenuCfg, "On-C64 SPACE-key menu for live scene tweaks."),
    (
        "wled",
        cfgmod.WledCfg,
        "Two-directional WLED bridge: broadcast SID audio-sync out (Mode 3) and/or "
        "act as a virtual WLED device the app can control (Mode 1).",
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
    (
        "wled",
        "Virtual WLED LED matrix — receive a realtime pixel stream (DDP / WLED "
        "UDP from LedFx/xLights) and render it to any display mode.",
        ("mhires", "hires", "hires_edges", "mcm", "petscii"),
    ),
)

# Scene type -> media_store.py kind(s) its `file =` field browses. Keys must
# equal the `file` FieldDoc's own `applies_to` (config.py's SceneCfg.file) —
# tests/test_introspect.py pins the two together so they can't drift.
# `generative` gets both kinds because which one applies depends on its own
# `audio_source` field, not on anything scene_types() can see; offering both is
# harmless since the loader (not the picker) is what actually enforces the
# match.
_SCENE_MEDIA_KINDS: dict[str, tuple[str, ...]] = {
    "video": ("video",),
    "waveform": ("sid",),
    "slideshow": ("picture",),
    "launcher": ("program",),
    "generative": ("sid", "audio"),
}


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
        if md.get("internal"):
            # Non-config tracking fields (e.g. MidiControlCfg.cc_map_is_default) —
            # never emitted to --describe / the schema / the serialized TOML.
            continue
        out.append(
            FieldDoc(
                name=f.name,
                type=str(f.type),
                default=getattr(blank, f.name),
                help=md.get("help", ""),
                choices=tuple(md.get("choices", ())),
                applies_to=tuple(md.get("applies_to", ())),
                apply=md.get("apply", "rebuild"),
                vocabulary=md.get("vocabulary", ""),
            )
        )
    return out


def config_sections() -> list[SectionDoc]:
    return [
        SectionDoc(
            name=name,
            help=help_,
            fields=tuple(_field_docs(dc)),
            reload=name in cfgmod.RELOADABLE_SECTIONS,
        )
        for name, dc, help_ in _SECTIONS
    ]


def display_modes() -> list[ModeDoc]:
    return list(_MODES)


# Live-tune target holders, in picker display order. Each pairs a cc_map "holder"
# prefix (the string before the "." in a param target — see
# midi_control._apply_param) with the picker section it lands in.
_LIVE_TARGET_GROUPS: tuple[tuple[str, str], ...] = (
    ("mode", "Color pipeline"),
    ("effect", "Effect"),
    ("source", "Generator"),
    ("scene", "Scope"),
)


def palette_swatches() -> list[dict[str, Any]]:
    """The sixteen C64 colors as ``{index, name, label, hex}``.

    ``name`` is the spelling a config should be written with and ``label`` the
    one to show; both round-trip through ``palette.resolve_color``. ``hex`` is
    read from the *live* table rather than the Pepto constant, so a host that
    has matched the machine's own palette offers swatches in the colors that
    machine actually emits. Imported lazily — palette pulls in numpy/cv2, and
    the ``--describe`` path never asks for this."""
    from c64cast.video.palette import C64_COLOR_NAMES, C64_COLORS, C64_PALETTE_BGR

    write_names = {index: name for name, index in C64_COLORS.items()}
    out: list[dict[str, Any]] = []
    for index, label in enumerate(C64_COLOR_NAMES):
        blue, green, red = (int(c) for c in C64_PALETTE_BGR[index])
        out.append(
            {
                "index": index,
                "name": write_names[index],
                "label": label,
                "hex": f"#{red:02x}{green:02x}{blue:02x}",
            }
        )
    return out


def _iter_live_holders() -> list[tuple[str, str, type]]:
    """Yield ``(holder, owner_name, cls)`` for every class that declares a
    ``LIVE_PARAMS``/``LIVE_CHOICES`` live-tune surface. Imported lazily (modes /
    effects / generators / voice_scope pull in numpy/cv2) so this module stays
    import-light for the schema / --describe path, which never calls it."""
    from c64cast.scenes import effects, generators
    from c64cast.sid import voice_scope
    from c64cast.video import modes as modesmod

    out: list[tuple[str, str, type]] = []

    # Display modes: every concrete DisplayMode subclass, by DisplayMode.name.
    def _walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            name = getattr(sub, "name", None)
            if isinstance(name, str) and name:
                out.append(("mode", name, sub))
            _walk(sub)

    _walk(modesmod.DisplayMode)
    for reg_name, cls in effects.REGISTRY.items():
        out.append(("effect", reg_name, cls))
    for reg_name, cls in generators.REGISTRY.items():
        out.append(("source", reg_name, cls))
    # The scope scenes (WaveformScene/MidiScene) mix in VoiceScopeRenderer, whose
    # LIVE_PARAMS live on the scene itself → the "scene." holder.
    out.append(("scene", "voice_scope", voice_scope.VoiceScopeRenderer))
    return out


# Choice targets whose values are C64 color names rather than a mode keyword —
# keyed by the bare param name (unambiguous across holders today). The picker
# renders these as swatches instead of a <select>; see LiveTargetDoc.vocabulary.
_LIVE_CHOICE_VOCAB: dict[str, str] = {"border": "c64color", "background": "c64color"}


def live_targets() -> list[LiveTargetDoc]:
    """Every live-tunable ``param`` target, deduped by ``holder.name`` and grouped
    for the ``--midi-setup`` target picker. Single source of truth over the
    ``LIVE_PARAMS`` (scalars) + ``LIVE_CHOICES`` (choices) class attributes on the
    effect / generator / mode / scope registries — a drift test pins this to those
    attrs (same spirit as the ``LIVE_CHOICES`` ↔ ``[color]`` metadata pin)."""
    group_of = dict(_LIVE_TARGET_GROUPS)
    # target -> mutable accumulator (kind/range/choices from the first declarer;
    # owners unioned across every class that declares the same holder.name).
    acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for holder, owner, cls in _iter_live_holders():
        params: dict[str, tuple[float, float]] = getattr(cls, "LIVE_PARAMS", {}) or {}
        choices_map: dict[str, tuple[str, ...]] = getattr(cls, "LIVE_CHOICES", {}) or {}
        for pname, (lo, hi) in params.items():
            target = f"{holder}.{pname}"
            if target not in acc:
                acc[target] = {
                    "holder": holder,
                    "kind": "scalar",
                    "lo": float(lo),
                    "hi": float(hi),
                    "choices": (),
                    "owners": [],
                }
                order.append(target)
            acc[target]["owners"].append(owner)
        for pname, values in choices_map.items():
            target = f"{holder}.{pname}"
            if target not in acc:
                acc[target] = {
                    "holder": holder,
                    "kind": "choice",
                    "lo": None,
                    "hi": None,
                    "choices": tuple(values),
                    "owners": [],
                    "vocabulary": _LIVE_CHOICE_VOCAB.get(pname, ""),
                }
                order.append(target)
            acc[target]["owners"].append(owner)

    out: list[LiveTargetDoc] = []
    for target in order:
        a = acc[target]
        holder = str(a["holder"])
        out.append(
            LiveTargetDoc(
                target=target,
                holder=holder,
                group=group_of.get(holder, holder),
                kind=str(a["kind"]),
                lo=a["lo"],
                hi=a["hi"],
                choices=a["choices"],
                owners=tuple(dict.fromkeys(a["owners"])),  # dedup, preserve order
                vocabulary=str(a.get("vocabulary", "")),
            )
        )
    # Stable group order for the picker (Color pipeline / Effect / Generator /
    # Scope), then insertion order within a group.
    group_rank = {g: i for i, (_, g) in enumerate(_LIVE_TARGET_GROUPS)}
    out.sort(key=lambda t: group_rank.get(t.group, len(group_rank)))
    return out


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
        out.append(
            SceneTypeDoc(
                name=name,
                help=help_,
                displays=displays,
                fields=relevant,
                media_kinds=_SCENE_MEDIA_KINDS.get(name, ()),
            )
        )
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
                default=p.default if has_default else REQUIRED,
                required=not has_default,
                help=help_map.get(pname, ""),
            )
        )
    return out


def overlay_docs() -> list[OverlayDoc]:
    out: list[OverlayDoc] = []
    overlay_classes = ovmod.overlay_types()
    for name in sorted(overlay_classes):
        cls = overlay_classes[name]
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
    return ovmod.known_overlays()


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
# JSON (the web console's copy of this model)
# ---------------------------------------------------------------------------


def _jsonable(val: object) -> object:
    """Coerce a default to something `json` can carry. Only two shapes need it:
    the ``REQUIRED`` sentinel, and the tuples/lists a list-valued field
    defaults to."""
    if val is REQUIRED:
        return None
    if isinstance(val, (tuple, list)):
        return [_jsonable(v) for v in val]
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    return str(val)


def as_dict() -> dict[str, Any]:
    """The whole introspection model as JSON-serializable data.

    The web console renders this rather than the committed JSON Schema because
    the schema deliberately drops the three things a UI needs most: ``apply``
    (does changing this take effect live, or does it need a rebuild?),
    ``applies_to`` (which scene types is this field even meaningful for), and
    ``vocabulary`` (what a free-text field's strings are drawn from). A plain
    ``dataclasses.asdict`` would carry them, but it would also emit the
    ``REQUIRED`` sentinel, which `json` can't encode.

    ``palette`` rides along for the same reason: a swatch picker over the C64
    colors needs the colors, and a browser deriving them from a copy of the
    palette would be a second one to keep in step."""

    def field_dict(fd: FieldDoc) -> dict[str, Any]:
        return {
            "name": fd.name,
            "type": fd.type,
            "default": _jsonable(fd.default),
            "required": fd.default is REQUIRED,
            "help": fd.help,
            "choices": list(fd.choices),
            "applies_to": list(fd.applies_to),
            "apply": fd.apply,
            "vocabulary": fd.vocabulary,
        }

    def param_dict(pd: ParamDoc) -> dict[str, Any]:
        return {
            "name": pd.name,
            "type": pd.type,
            "default": _jsonable(pd.default),
            "required": pd.required,
            "help": pd.help,
        }

    return {
        "sections": [
            {
                "name": s.name,
                "help": s.help,
                "reload": s.reload,
                "fields": [field_dict(f) for f in s.fields],
            }
            for s in config_sections()
        ],
        "scene_types": [
            {
                "name": s.name,
                "help": s.help,
                "displays": list(s.displays),
                "fields": [field_dict(f) for f in s.fields],
                "media_kinds": list(s.media_kinds),
            }
            for s in scene_types()
        ],
        "overlays": [
            {
                "name": o.name,
                "help": o.help,
                "params": [param_dict(p) for p in o.params],
                "requires_petscii": o.requires_petscii,
                "requires_audio": o.requires_audio,
                "compatible_modes": list(o.compatible_modes),
                "supports_bitmap_text": o.supports_bitmap_text,
            }
            for o in overlay_docs()
        ],
        "modes": [
            {
                "name": m.name,
                "runtime_name": m.runtime_name,
                "is_bitmapped": m.is_bitmapped,
                "is_petscii_compatible": m.is_petscii_compatible,
                "is_bitmap_text_compatible": m.is_bitmap_text_compatible,
                "help": m.help,
            }
            for m in display_modes()
        ],
        "live_targets": [
            {
                "target": t.target,
                "holder": t.holder,
                "group": t.group,
                "kind": t.kind,
                "lo": t.lo,
                "hi": t.hi,
                "choices": list(t.choices),
                "owners": list(t.owners),
            }
            for t in live_targets()
        ],
        "palette": palette_swatches(),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_default(val: object) -> str:
    if val is REQUIRED:
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


# ---------------------------------------------------------------------------
# Packaged example configs
# ---------------------------------------------------------------------------
#
# `--list-examples` reads the shipped files rather than a hand-kept table, for
# the same reason the rest of this module reads config metadata: the old
# Markdown index in examples/README.md had drifted ~15 files behind. A
# generated index cannot.

_SUMMARY_MAX = 150


def example_summary(path: Path) -> str:
    """One-line summary of an example config, read from its own header: the
    leading comment paragraph (skipping the `#:schema` directive, stopping at
    the first blank comment line), first sentence only.

    The `Single-scene demo:` prefix nearly every file opens with is dropped —
    it is true of all but a handful and repeating it 45 times crowds out the
    part that differs. So are the `(see <upstream URL>)` citations the
    WLED-effect ports carry: provenance belongs in the file, not in an index."""
    para: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#:schema"):
                continue
            if not stripped.startswith("#"):
                break
            body = stripped[1:].strip()
            if not body:
                break
            para.append(body)
    text = re.sub(r"\s+", " ", " ".join(para))
    text = re.sub(r"\s*\(see [^)]*\)", "", text)
    # First sentence. A period only ends one when whitespace follows, which
    # spares `FX.cpp` and `docs/caveats.md` mid-sentence.
    if match := re.search(r"\.(?=\s)", text):
        text = text[: match.end()]
    text = re.sub(r"^Single-scene demo(?: of|:)\s*", "", text)
    # A few files open with one very long sentence; hold every entry to about
    # two terminal lines so the list stays scannable.
    if len(text) > _SUMMARY_MAX:
        text = text[:_SUMMARY_MAX].rsplit(" ", 1)[0] + " …"
    return text


def example_needs_media(path: Path) -> bool:
    """True when a *scene* in this example sources from `assets/` — the
    directory that ships empty (user media, unclear licensing), so the demo
    needs a file dropped in or its `file =` repointed before it will run.

    Only a scene's own `file` counts. An overlay's (the `logo` one) falls back
    to a drawn placeholder when the file is missing, so those demos run as
    shipped and must not be tagged."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        return False
    return any(
        isinstance(sc, dict)
        and isinstance(spec := sc.get("file"), str)
        and spec.startswith("assets/")
        for sc in scenes
    )


def _example_display_order(paths: list[Path]) -> list[Path]:
    """`hello` first (the documented first run), then the annotated reference,
    then everything else in `paths` order."""
    lead = ["hello", "c64cast.example"]
    ranked = {name: i for i, name in enumerate(lead)}
    return sorted(paths, key=lambda p: (ranked.get(pathsmod.example_name(p), len(lead)),))


def render_list_examples() -> str:
    """Render the packaged example configs — name, then the summary from each
    file's own header. Names are what `--config example:<name>` takes."""
    paths = _example_display_order(pathsmod.example_config_paths())
    names = [pathsmod.example_name(p) for p in paths]
    name_w = max((len(n) for n in names), default=8)
    lines = [
        "Example configs shipped with c64cast (each a runnable single-scene demo unless noted):",
        "",
    ]
    for path, name in zip(paths, names, strict=True):
        tag = " [needs your own media]" if example_needs_media(path) else ""
        summary = example_summary(path) + tag
        # Don't break on hyphens: several summaries carry upstream URLs.
        wrapped = textwrap.wrap(
            summary, width=max(40, 98 - name_w - 4), break_on_hyphens=False
        ) or [""]
        lines.append(f"  {name:<{name_w}}  {wrapped[0]}")
        lines += [" " * (name_w + 4) + cont for cont in wrapped[1:]]
    lines += [
        "",
        "Run one:   c64cast --config example:<name>",
        "Copy one:  c64cast --print-example <name> > c64cast.toml",
    ]
    return "\n".join(lines)


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
    # Only worth saying when some overlay actually carries the gate — the
    # spectrum overlays used to and no longer do (they read the scene's music
    # features first), so this note would otherwise be a lie by default.
    if any(ov.requires_audio for ov, _ in rows):
        lines.append("Note: audio overlays additionally need [audio].enabled.")
    return "\n".join(lines)
