"""Per-scene recording metadata for sharing "how was this configured?" info.

`Playlist._safe_setup` calls `log_scene_recording_metadata` once per scene
activation (including loop re-entries and random-pool re-picks). It logs one
JSON line tagged with `SCENE_CONFIG_MARKER` — a snapshot of that scene's
*coalesced* settings (the same defaults→config→CLI precedence
`config.merge_cli` already applies) plus scene-specific source metadata.
`scripts/scene_config_to_description.py` parses a log file for these lines
and renders a pasteable YouTube-description blob via `render_description`.

Two things are deliberately excluded from the payload, since it's meant to
end up in a public video description:

- Machine-identifying connection info (`[ultimate64].url`/`dma_password`,
  `[teensyrom].host`/`serial_port`). Only the backend kind + NTSC/PAL +
  sid_model are surfaced from that area.
- Any live hardware read. Everything here comes from the already-resolved
  `Config`/`SceneCfg`/scene instance state — no extra U64 traffic, which
  matters mid-recording.

Video scenes get a `copyright` **placeholder** rather than a guess — c64cast
doesn't collect yt-dlp uploader/license data today (see the note in
docs/architecture.md). Waveform / generative-sid scenes are different: the
PSID header (`SidHeader.name`/`author`/`released`) usually already carries
real title/author/copyright text, so that's used verbatim.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import math
import os
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .config import Config
    from .scenes import Scene

log = logging.getLogger("c64cast.recording")

# Prefixes every SCENE_CONFIG log line so the parser can find it independent
# of the active log formatter (rich/plain terminal vs. file handler).
SCENE_CONFIG_MARKER = "SCENE_CONFIG_JSON"

_PLACEHOLDER_COPYRIGHT = "TODO: add source link / license / attribution"

# SceneCfg fields already surfaced elsewhere in the payload in resolved form
# (scene.name, scene.display_mode, scene.duration_s, scene.target_fps,
# scene.audio, scene.effect, scene.overlays, source.*) — skipped here so the
# blob doesn't show a raw value next to its resolved counterpart.
_SCENE_CFG_SKIP_FIELDS = frozenset(
    {"type", "name", "display", "duration_s", "file", "target_fps", "audio", "effect", "overlays"}
)

_AUDIO_FIELDS = (
    "enabled",
    "backend",
    "dac_curve",
    "sampler_sample_rate",
    "sampler_bits",
    "sampler_clock_hz",
    "dac_bitmap_tempo_hires",
    "dac_bitmap_tempo_mhires",
    "pitch_mult_petscii",
    "pitch_mult_hires",
    "pitch_mult_mhires",
    "pitch_mult_mcm",
    "pitch_mult_blank",
)


def _scene_cfg_fields(scene_cfg: Any) -> dict[str, Any]:
    """SceneCfg fields relevant to its own type (via `applies_to` metadata,
    same filter idiom as `introspect.scene_types`), minus the ones already
    surfaced in resolved form elsewhere in the payload."""
    if scene_cfg is None:
        return {}
    scene_type = getattr(scene_cfg, "type", None)
    out: dict[str, Any] = {}
    for fd in dataclasses.fields(scene_cfg):
        if fd.name in _SCENE_CFG_SKIP_FIELDS:
            continue
        applies_to = fd.metadata.get("applies_to")
        if applies_to and scene_type not in applies_to:
            continue
        out[fd.name] = getattr(scene_cfg, fd.name)
    return out


def _sid_header_fields(header: Any) -> dict[str, str]:
    return {
        "sid_name": getattr(header, "name", "") if header is not None else "",
        "sid_author": getattr(header, "author", "") if header is not None else "",
        "sid_released": getattr(header, "released", "") if header is not None else "",
    }


def _video_source(scene: Scene) -> dict[str, Any]:
    scene_cfg = getattr(scene, "_cfg", None)
    raw_spec = getattr(scene_cfg, "file", None) if scene_cfg is not None else None
    filepath = getattr(scene, "filepath", None)
    is_url = isinstance(raw_spec, str) and raw_spec.lower().startswith(("http://", "https://"))
    return {
        "url": raw_spec if is_url else None,
        "local_file": None if is_url or not filepath else os.path.basename(filepath),
        "title": getattr(scene, "name", None),
        "copyright": _PLACEHOLDER_COPYRIGHT,
    }


def _waveform_source(scene: Scene) -> dict[str, Any]:
    out: dict[str, Any] = dict(_sid_header_fields(getattr(scene, "header", None)))
    sid_file = getattr(scene, "_sid_file", None)
    out["local_file"] = os.path.basename(sid_file) if sid_file else None
    return out


def _generative_source(scene: Scene) -> dict[str, Any]:
    audio_source = getattr(scene, "audio_source", None)
    header = getattr(audio_source, "header", None)
    if header is None:
        return {"note": "no SID audio_source for this generative scene; nothing to attribute"}
    out: dict[str, Any] = dict(_sid_header_fields(header))
    sid_file = getattr(audio_source, "_sid_file", None)
    out["local_file"] = os.path.basename(sid_file) if sid_file else None
    return out


def _generic_source(scene: Scene) -> dict[str, Any]:
    scene_cfg = getattr(scene, "_cfg", None)
    file_spec = getattr(scene_cfg, "file", None) if scene_cfg is not None else None
    return {"file_spec": file_spec} if file_spec else {}


_SOURCE_BUILDERS = {
    "video": _video_source,
    "waveform": _waveform_source,
    "generative": _generative_source,
}


def _scene_source(scene: Scene, scene_type: str | None) -> dict[str, Any]:
    builder = _SOURCE_BUILDERS.get(scene_type or "", _generic_source)
    return builder(scene)


def build_scene_recording_metadata(scene: Scene, cfg: Config, system_name: str) -> dict[str, Any]:
    """Assemble one scene-start snapshot: coalesced config + scene metadata.

    Safe to call repeatedly (every scene activation) — pure read of already-
    resolved state, no I/O."""
    scene_cfg = getattr(scene, "_cfg", None)
    scene_type = getattr(scene_cfg, "type", None)
    display_mode = getattr(scene, "display_mode", None)
    duration_s = getattr(scene, "duration_s", None)
    overlay_names = [
        getattr(ov, "name", type(ov).__name__) for ov in getattr(scene, "overlays", ())
    ]

    return {
        "event": "scene_config",
        "meta": {
            "c64cast_version": __version__,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "system_name": system_name,
        },
        "scene": {
            "type": scene_type,
            "name": getattr(scene, "name", None),
            "display_mode": type(display_mode).__name__ if display_mode is not None else "none",
            "duration_s": (
                "unbounded"
                if isinstance(duration_s, float) and math.isinf(duration_s)
                else duration_s
            ),
            "target_fps": getattr(scene, "target_fps", None) or "auto",
            "audio_enabled": getattr(scene, "audio", None) is not None,
            "effect": type(getattr(scene, "effect", None)).__name__
            if getattr(scene, "effect", None) is not None
            else None,
            "overlays": overlay_names,
            **_scene_cfg_fields(scene_cfg),
        },
        "source": _scene_source(scene, scene_type),
        "color": dataclasses.asdict(cfg.color),
        "audio": {name: getattr(cfg.audio, name) for name in _AUDIO_FIELDS},
        "hardware": {
            "backend": cfg.hardware.backend,
            "system": cfg.ultimate64.system,
            "sid_model": cfg.ultimate64.sid_model,
        },
    }


def log_scene_recording_metadata(scene: Scene, cfg: Config | None, system_name: str) -> None:
    """Build + log one SCENE_CONFIG_JSON line for `scene`. No-ops quietly if
    `cfg` is unavailable; never raises (a bug here must not interrupt a live
    recording)."""
    if cfg is None:
        return
    try:
        payload = build_scene_recording_metadata(scene, cfg, system_name)
        log.info("%s %s", SCENE_CONFIG_MARKER, json.dumps(payload))
    except Exception:
        log.exception(
            "failed to log scene recording metadata for %r", getattr(scene, "name", scene)
        )


def extract_scene_configs(text: str) -> list[dict[str, Any]]:
    """Pull every SCENE_CONFIG_JSON payload out of log text, in order.
    Tolerant of surrounding formatter noise (timestamps, logger names) and
    of lines that fail to parse (e.g. a manually truncated log)."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        idx = line.find(SCENE_CONFIG_MARKER)
        if idx == -1:
            continue
        payload_text = line[idx + len(SCENE_CONFIG_MARKER) :].strip()
        try:
            out.append(json.loads(payload_text))
        except json.JSONDecodeError:
            continue
    return out


def render_description(payload: dict[str, Any]) -> str:
    """Render one scene_config payload as a human, paste-ready text block."""
    scene = payload.get("scene", {})
    source = payload.get("source", {})
    color = payload.get("color", {})
    audio = payload.get("audio", {})
    hardware = payload.get("hardware", {})
    meta = payload.get("meta", {})

    lines: list[str] = []
    lines.append(f"Scene: {scene.get('name', '?')} ({scene.get('type', '?')})")
    lines.append(f"Display mode: {scene.get('display_mode', '?')}")
    lines.append(
        f"Hardware: {hardware.get('backend', '?')}, {hardware.get('system', '?')}"
        f", sid_model={hardware.get('sid_model', '?')}"
    )
    lines.append(
        "Color: dither={dither} color_match={color_match} cell_strategy={cell_strategy} "
        "motion_smoothing={motion_smoothing} auto_fit={auto_fit}".format(**color)
    )
    lines.append(
        f"Audio: backend={audio.get('backend', '?')} dac_curve={audio.get('dac_curve', '?')}"
    )

    if source.get("url"):
        lines.append(f"Source video: {source['url']}")
        lines.append(f"Copyright: {source.get('copyright', _PLACEHOLDER_COPYRIGHT)}")
    elif source.get("local_file"):
        lines.append(f"Source file: {source['local_file']}")
        if "copyright" in source:
            lines.append(f"Copyright: {source['copyright']}")
        elif source.get("sid_author") or source.get("sid_released"):
            lines.append(
                f"SID: {source.get('sid_name', '')} by {source.get('sid_author', '')} "
                f"({source.get('sid_released', '')})"
            )

    lines.append("")
    lines.append(f"-- generated with c64cast {meta.get('c64cast_version', '?')}")
    lines.append(f"-- recorded {meta.get('timestamp', '?')}")
    return "\n".join(lines)
