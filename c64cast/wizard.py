"""Interactive config builder — ``c64cast --init``.

A fourth surface over the single-source-of-truth config metadata (after
``introspect`` / ``schema`` / ``config_serialize``): instead of *rendering* the
model for reading, this *drives prompts* from it. Scene/overlay choices,
per-field defaults, valid ``choices``, and overlay×display compatibility all
come from the same ``introspect`` model, so the wizard can't offer an option
the loader would reject — and it can't drift from ``--describe``.

Two build modes:

* **Single scene** (the shape every ``config/examples/`` file uses, which the
  Playlist runs in single-scene loop mode) — a scene plus its overlays and the
  essential globals (U64 URL, video system, audio).
* **Multi-scene playlist** — several scenes in order with the "UP NEXT"
  interstitial, optional video interleaving, and loop-vs-play-once behavior.

Either result is validated with ``config.validate_scene_cfg`` and written via
``config_serialize.dumps`` as annotated, ``#:schema``-tagged TOML. The serializer
already round-trips N ``[[scenes]]`` + ``[playlist]``/``[interstitial]`` sections
(``load(dumps(cfg)) == cfg``), so multi-scene is purely a wizard-flow extension.

The questionary I/O is a thin shell around pure helpers (``make_scene`` /
``build_config`` / ``build_multi_config`` / ``validate_all`` /
``compatible_overlays`` / ``scan_assets`` / ``schema_directive_for``) so the
buildable logic is unit-testable without a terminal — mirroring how
``vision.py`` keeps its pose classifiers pure and camera-free.
"""

from __future__ import annotations

import os

from . import config as cfgmod
from . import config_serialize as ser
from . import introspect

# Scene type -> (default asset dir, accepted extensions) for the file picker.
# Mirrors the DEFAULT_*_DIR / *_EXTS constants the loader resolves against, so
# the wizard suggests exactly the directory build_scene will search.
_ASSET_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "video": (cfgmod.DEFAULT_VIDEO_DIR, cfgmod.VIDEO_EXTS),
    "waveform": (cfgmod.DEFAULT_WAVEFORM_DIR, cfgmod.SID_EXTS),
    "slideshow": (cfgmod.DEFAULT_SLIDESHOW_DIR, cfgmod.PICTURE_EXTS),
    "launcher": (cfgmod.DEFAULT_PROGRAM_DIR, cfgmod.PROGRAM_EXTS),
}

# Scene-field names handled explicitly by the guided flow; the "advanced" walk
# covers everything else applicable to the chosen type.
_CORE_SCENE_FIELDS = frozenset(
    {"type", "display", "name", "duration_s", "file", "overlays", "audio"}
)

# Scene types whose audio is driven by the scene/program itself, not the
# global [audio] streamer — the wizard doesn't ask "enable audio?" for these.
_SELF_AUDIO_TYPES = frozenset({"waveform", "midi", "asid", "launcher"})


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without questionary)
# ---------------------------------------------------------------------------


def field_kind(type_str: str) -> str:
    """Classify a dataclass/param annotation string into a prompt kind:
    'bool' | 'int' | 'float' | 'str' | 'complex' (list/dict — skipped in the
    generic walk). Order matters: bool before int (bool is an int subclass in
    Python, and "bool" doesn't contain "int" anyway, but float before int so
    'float' isn't misread)."""
    t = type_str.lower()
    if "list" in t or "dict" in t:
        return "complex"
    if "bool" in t:
        return "bool"
    if "float" in t:
        return "float"
    if "int" in t:
        return "int"
    return "str"


def scan_assets(default_dir: str, exts: tuple[str, ...]) -> list[str]:
    """List existing files in `default_dir` whose extension is in `exts`,
    sorted. Non-recursive (mirrors the loader's directory specs). Returns []
    if the directory is missing — the caller falls back to free-text entry."""
    if not os.path.isdir(default_dir):
        return []
    return sorted(
        os.path.join(default_dir, f)
        for f in os.listdir(default_dir)
        if os.path.isfile(os.path.join(default_dir, f)) and f.lower().endswith(exts)
    )


def _mode_runtime_name(display: str) -> str:
    """Map a `display` config value to the DisplayMode.name overlays match
    against (hires_edges + hires both -> 'hires'). Falls back to the value
    itself for unknown displays."""
    for m in introspect.display_modes():
        if m.name == display:
            return m.runtime_name
    return display


def compatible_overlays(display: str, *, audio_enabled: bool) -> list[introspect.OverlayDoc]:
    """Overlays that attach to a scene painting `display`, honoring the same
    rules as ``overlays.validate_for_scene`` (via ``introspect.overlay_mode_ok``)
    plus the audio requirement. This is what keeps the wizard from offering an
    overlay the loader would reject."""
    runtime = _mode_runtime_name(display)
    modes = {m.runtime_name: m for m in introspect.display_modes()}
    mode = modes.get(runtime)
    out: list[introspect.OverlayDoc] = []
    for ov in introspect.overlay_docs():
        if mode is not None and not introspect.overlay_mode_ok(ov, mode)[0]:
            continue
        if ov.requires_audio and not audio_enabled:
            continue
        out.append(ov)
    return out


def supported_displays(scene_type: str) -> tuple[str, ...]:
    """The `display` values a scene type accepts (empty = the type fixes or
    ignores display). Straight from the introspect model."""
    for st in introspect.scene_types():
        if st.name == scene_type:
            return st.displays
    return ()


def scene_field_docs(scene_type: str) -> tuple[introspect.FieldDoc, ...]:
    for st in introspect.scene_types():
        if st.name == scene_type:
            return st.fields
    return ()


def make_scene(
    scene_type: str, scene_fields: dict[str, object], overlays: list[dict[str, object]]
) -> cfgmod.SceneCfg:
    """Build one SceneCfg from collected answers (field setattr loop + overlay
    copy). Pure; shared by the single- and multi-scene build paths."""
    scene = cfgmod.SceneCfg(type=scene_type)
    for name, value in scene_fields.items():
        setattr(scene, name, value)
    scene.overlays = [dict(ov) for ov in overlays]
    return scene


def _apply_globals(
    cfg: cfgmod.Config,
    *,
    url: str | None = None,
    system: str | None = None,
    audio_enabled: bool | None = None,
    vision_enabled: bool | None = None,
    audio_overrides: dict[str, object] | None = None,
) -> None:
    """Overlay the essential global settings onto a Config (in place). Each is
    skipped when None so callers can leave any at its dataclass default.
    `audio_overrides` is a `{field: value}` map setattr'd onto cfg.audio (e.g.
    backend / sampler_sample_rate / sampler_bits)."""
    if url:
        cfg.ultimate64.url = url
    if system:
        cfg.ultimate64.system = system
    if audio_enabled is not None:
        cfg.audio.enabled = audio_enabled
    if vision_enabled is not None:
        cfg.vision.enabled = vision_enabled
    for key, value in (audio_overrides or {}).items():
        setattr(cfg.audio, key, value)


def build_config(
    *,
    scene_type: str,
    scene_fields: dict[str, object],
    overlays: list[dict[str, object]],
    url: str | None = None,
    system: str | None = None,
    audio_enabled: bool | None = None,
    vision_enabled: bool | None = None,
    audio_overrides: dict[str, object] | None = None,
) -> cfgmod.Config:
    """Assemble a single-scene Config from collected answers. Pure — no I/O —
    so the wizard's terminal shell stays a thin layer over this."""
    cfg = cfgmod.Config()
    _apply_globals(
        cfg,
        url=url,
        system=system,
        audio_enabled=audio_enabled,
        vision_enabled=vision_enabled,
        audio_overrides=audio_overrides,
    )
    cfg.scenes = [make_scene(scene_type, scene_fields, overlays)]
    return cfg


def build_multi_config(
    *,
    scenes: list[cfgmod.SceneCfg],
    url: str | None = None,
    system: str | None = None,
    audio_enabled: bool | None = None,
    vision_enabled: bool | None = None,
    audio_overrides: dict[str, object] | None = None,
    playlist: dict[str, object] | None = None,
    interstitial: dict[str, object] | None = None,
) -> cfgmod.Config:
    """Assemble a multi-scene Config. `scenes` is set verbatim (order
    preserved); `playlist`/`interstitial` are dict overrides applied via
    setattr onto the matching section. Pure — no I/O."""
    cfg = cfgmod.Config()
    _apply_globals(
        cfg,
        url=url,
        system=system,
        audio_enabled=audio_enabled,
        vision_enabled=vision_enabled,
        audio_overrides=audio_overrides,
    )
    cfg.scenes = list(scenes)
    for key, value in (playlist or {}).items():
        setattr(cfg.playlist, key, value)
    for key, value in (interstitial or {}).items():
        setattr(cfg.interstitial, key, value)
    return cfg


def validate(cfg: cfgmod.Config) -> str | None:
    """Run the loader's pre-construction validation on the wizard's single
    scene. Returns an error message string on failure, else None."""
    try:
        cfgmod.validate_scene_cfg(cfg.scenes[0], cfg, audio_enabled=cfg.audio.enabled)
    except Exception as e:  # ValueError / OrchestratorError — surface verbatim
        return str(e)
    return None


def validate_all(cfg: cfgmod.Config) -> list[str]:
    """Validate every scene in a (multi-scene) Config, collecting one
    ``scene N (name): <err>`` message per failure. Returns [] when all valid."""
    errs: list[str] = []
    for i, s in enumerate(cfg.scenes):
        try:
            cfgmod.validate_scene_cfg(s, cfg, audio_enabled=cfg.audio.enabled)
        except Exception as e:  # ValueError / OrchestratorError
            errs.append(f"scene {i + 1} ({s.name or s.type}): {e}")
    return errs


def _section_field_docs(section_name: str) -> tuple[introspect.FieldDoc, ...]:
    """FieldDocs for a config section (e.g. 'interstitial'), straight from the
    introspect model so prompts/choices/defaults can't drift from the code."""
    for sec in introspect.config_sections():
        if sec.name == section_name:
            return sec.fields
    return ()


def schema_directive_for(out_path: str) -> str:
    """Best-effort relative path from the output file's directory to the
    committed ``c64cast.schema.json`` (walking up from cwd), so the written
    ``#:schema`` line points at the real schema for editor autocomplete.
    Falls back to the serializer default when the schema isn't found."""
    schema_name = "c64cast.schema.json"
    out_dir = os.path.dirname(os.path.abspath(out_path))
    cur = os.getcwd()
    while True:
        candidate = os.path.join(cur, schema_name)
        if os.path.isfile(candidate):
            rel = os.path.relpath(candidate, out_dir)
            # Keep "./foo" style for same-dir for readability.
            return rel if rel.startswith(("..", os.sep)) else f".{os.sep}{rel}"
        parent = os.path.dirname(cur)
        if parent == cur:
            return ser.DEFAULT_SCHEMA_PATH
        cur = parent


# ---------------------------------------------------------------------------
# Interactive shell (thin; not unit-tested)
# ---------------------------------------------------------------------------


def _ensure_questionary():  # type: ignore[no-untyped-def]
    """Lazy-import questionary so the dep is only needed for `--init` (mirrors
    video.py / vision.py lazy-importing their extras)."""
    try:
        import questionary

        return questionary
    except ImportError:
        return None


def _coerce(kind: str, raw: str) -> object:
    raw = raw.strip()
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def _prompt_typed(
    q,
    *,
    label: str,
    kind: str,
    default: object,  # type: ignore[no-untyped-def]
    choices: tuple[str, ...],
    required: bool,
    help_: str,
):
    """Prompt one scalar value. Returns the value, or the sentinel `...`
    (Ellipsis) meaning "leave at default / omit"."""
    instruction = f"  {help_}" if help_ else None
    if choices:
        return q.select(
            label,
            choices=list(choices),
            default=default if default in choices else None,
            instruction=instruction,
        ).ask()
    if kind == "bool":
        return q.confirm(label, default=bool(default), instruction=instruction).ask()

    def _validate(text: str) -> bool | str:
        text = text.strip()
        if not text:
            return "required" if required else True
        try:
            _coerce(kind, text)
        except ValueError:
            return f"enter a valid {kind}"
        return True

    shown_default = "" if default is None else str(default)
    answer = q.text(label, default=shown_default, validate=_validate, instruction=instruction).ask()
    if answer is None:  # Ctrl-C
        return None
    if not answer.strip():
        return ...  # leave at default
    return _coerce(kind, answer)


def _prompt_scene_fields(q, scene_type: str) -> dict[str, object] | None:  # type: ignore[no-untyped-def]
    """Walk the type's non-core applicable fields (the 'advanced' set)."""
    collected: dict[str, object] = {}
    for fd in scene_field_docs(scene_type):
        if fd.name in _CORE_SCENE_FIELDS:
            continue
        kind = field_kind(fd.type)
        if kind == "complex":
            continue  # list/dict fields — hand-edit; out of scope for v1 walk
        val = _prompt_typed(
            q,
            label=fd.name,
            kind=kind,
            default=fd.default,
            choices=fd.choices,
            required=False,
            help_=fd.help,
        )
        if val is None:
            return None
        if val is not ... and val != fd.default:
            collected[fd.name] = val
    return collected


def _prompt_section_fields(q, section_name: str) -> dict[str, object] | None:  # type: ignore[no-untyped-def]
    """Walk a config section's scalar fields (e.g. [interstitial]); returns a
    dict of only the values the user changed from their defaults."""
    collected: dict[str, object] = {}
    for fd in _section_field_docs(section_name):
        kind = field_kind(fd.type)
        if kind == "complex":
            continue
        val = _prompt_typed(
            q,
            label=fd.name,
            kind=kind,
            default=fd.default,
            choices=fd.choices,
            required=False,
            help_=fd.help,
        )
        if val is None:
            return None
        if val is not ... and val != fd.default:
            collected[fd.name] = val
    return collected


def _prompt_overlay_params(q, ov: introspect.OverlayDoc) -> dict[str, object] | None:  # type: ignore[no-untyped-def]
    params: dict[str, object] = {"type": ov.name}
    for p in ov.params:
        kind = field_kind(p.type)
        if kind == "complex":
            continue
        default = None if p.default is introspect._REQUIRED else p.default
        val = _prompt_typed(
            q,
            label=f"{ov.name}.{p.name}",
            kind=kind,
            default=default,
            choices=(),
            required=p.required,
            help_=p.help,
        )
        if val is None:
            return None
        if val is ...:
            continue  # leave optional param at its default
        params[p.name] = val
    return params


def _pick_asset(q, scene_type: str) -> str | None:  # type: ignore[no-untyped-def]
    default_dir, exts = _ASSET_SPECS[scene_type]
    files = scan_assets(default_dir, exts)
    use_dir = f"Use the whole {default_dir}/ directory (random each play)"
    custom = "Type a path / glob myself"
    if files:
        choices = [os.path.relpath(f) for f in files] + [use_dir, custom]
        pick = q.select(f"Pick a file for the {scene_type} scene", choices=choices).ask()
        if pick is None:
            return None
        if pick == use_dir:
            return default_dir
        if pick != custom:
            return pick
    # No files found, or user chose custom entry.
    return q.text(
        f"{scene_type} file spec (path / dir / comma-separated globs)",
        default="" if files else default_dir,
        validate=lambda t: True if t.strip() else "required",
        instruction=f"  accepted: {', '.join(exts)}",
    ).ask()


_SINGLE_LABEL = "Single scene (loops forever)"
_MULTI_LABEL = "Multi-scene playlist (UP NEXT between scenes)"


def _prompt_one_scene(
    q,
    *,
    audio_enabled: bool | None,  # type: ignore[no-untyped-def]
    ask_audio: bool,
) -> dict[str, object] | None:
    """Question one scene (type → file → display → name → audio → advanced →
    overlays). Returns ``{"scene_type","scene_fields","overlays",
    "audio_enabled"}`` or None on cancel.

    ``ask_audio=True`` (single-scene): asks the per-scene audio question and
    returns it as the global. ``ask_audio=False`` (multi-scene): the global
    audio is already chosen and passed in; when it's on and the type is
    streamer-driven, offers a per-scene mute that records ``audio=False`` into
    ``scene_fields`` (the existing SceneCfg.audio override)."""
    type_choices = [f"{st.name}  —  {st.help}" for st in introspect.scene_types()]
    raw = q.select("Scene type", choices=type_choices).ask()
    if raw is None:
        return None
    scene_type = raw.split("  —  ")[0]

    scene_fields: dict[str, object] = {}

    # --- file (asset-bearing types) ---
    if scene_type in _ASSET_SPECS:
        f = _pick_asset(q, scene_type)
        if f is None:
            return None
        scene_fields["file"] = f

    # --- display (display-bearing types) ---
    displays = supported_displays(scene_type)
    display_for_overlays = "petscii"  # default assumption for fixed-mode types
    if displays:
        chosen = q.select("Display mode", choices=list(displays), default=displays[0]).ask()
        if chosen is None:
            return None
        scene_fields["display"] = chosen
        display_for_overlays = chosen

    # --- name (optional) ---
    name = q.text("Scene name (optional, shown in logs/interstitials)", default="").ask()
    if name is None:
        return None
    if name.strip():
        scene_fields["name"] = name.strip()

    # --- audio ---
    if ask_audio:
        scene_audio: bool | None = None
        if scene_type not in _SELF_AUDIO_TYPES:
            scene_audio = q.confirm(
                "Enable SID audio streaming for this scene?", default=(scene_type == "video")
            ).ask()
            if scene_audio is None:
                return None
        result_audio = scene_audio
        audio_for_overlays = bool(scene_audio)
    else:
        result_audio = audio_enabled
        if audio_enabled and scene_type not in _SELF_AUDIO_TYPES:
            mute = q.confirm("Mute audio for this scene?", default=False).ask()
            if mute is None:
                return None
            if mute:
                scene_fields["audio"] = False
        audio_for_overlays = bool(audio_enabled) and scene_fields.get("audio") is not False

    # --- advanced fields ---
    if q.confirm(f"Configure advanced {scene_type} options?", default=False).ask():
        adv = _prompt_scene_fields(q, scene_type)
        if adv is None:
            return None
        scene_fields.update(adv)

    # --- overlays (compat-filtered) ---
    overlays: list[dict[str, object]] = []
    candidates = compatible_overlays(display_for_overlays, audio_enabled=audio_for_overlays)
    if candidates and q.confirm("Add overlays?", default=False).ask():
        labels = {f"{ov.name}  —  {ov.help}": ov for ov in candidates}
        picked = q.checkbox("Select overlays (space to toggle)", choices=list(labels)).ask()
        for lbl in picked or []:
            ov = labels[lbl]
            params = _prompt_overlay_params(q, ov)
            if params is None:
                return None
            overlays.append(params)

    return {
        "scene_type": scene_type,
        "scene_fields": scene_fields,
        "overlays": overlays,
        "audio_enabled": result_audio,
    }


def _prompt_audio_backend(q, audio_enabled: bool) -> dict[str, object] | None:  # type: ignore[no-untyped-def]
    """Ask the video-audio backend when audio is on (the wizard targets the
    Ultimate, which has the FPGA sampler). Returns an `audio_overrides` dict for
    `_apply_globals` ({} when audio is off / left at default), or None on cancel."""
    if not audio_enabled:
        return {}
    backend = q.select(
        "Video-audio backend (sampler = U64 Ultimate Audio FPGA PCM, hi-fi; "
        "dac = lo-fi 4-bit $D418)",
        choices=list(cfgmod._AUDIO_BACKEND_CHOICES),
        default="auto",
    ).ask()
    if backend is None:
        return None
    overrides: dict[str, object] = {"backend": backend}
    if backend == "sampler":
        bits = q.select("Sampler PCM bit depth", choices=["16", "8"], default="16").ask()
        if bits is None:
            return None
        rate = q.text("Sampler sample rate (Hz, 1000..48000)", default="44100").ask()
        if rate is None:
            return None
        try:
            overrides["sampler_sample_rate"] = int(rate)
        except ValueError:
            overrides["sampler_sample_rate"] = 44100
        overrides["sampler_bits"] = int(bits)
    return overrides


def _prompt_globals(q) -> tuple[str, str] | None:  # type: ignore[no-untyped-def]
    """Ask the U64 URL + video system. Returns (url, system) or None."""
    url = q.text("Ultimate 64 URL", default=cfgmod.Ultimate64Cfg().url).ask()
    if url is None:
        return None
    system = q.select("Video system", choices=list(cfgmod._SYSTEM_CHOICES), default="NTSC").ask()
    if system is None:
        return None
    return url, system


def _scene_label(scene: dict[str, object]) -> str:
    """Short human label for a collected scene dict (for summaries/pickers)."""
    fields = scene["scene_fields"]
    assert isinstance(fields, dict)
    name = fields.get("name")
    disp = fields.get("display")
    base = f"{scene['scene_type']} ({disp})" if disp else str(scene["scene_type"])
    return f"{base} — {name}" if name else base


def _pick_scene_index(
    q,
    scenes: list[dict[str, object]],  # type: ignore[no-untyped-def]
    prompt: str,
) -> int | None:
    choices = [f"{i + 1}. {_scene_label(s)}" for i, s in enumerate(scenes)]
    pick = q.select(prompt, choices=choices).ask()
    if pick is None:
        return None
    return choices.index(pick)


def _manage_playlist(
    q,
    *,
    audio_enabled: bool,  # type: ignore[no-untyped-def]
) -> list[dict[str, object]] | None:
    """Add/Remove/Move/Done loop building an ordered scene list. Returns the
    list (≥1 scene) or None on cancel."""
    scenes: list[dict[str, object]] = []
    while True:
        if scenes:
            print("\nPlaylist so far:")
            for i, s in enumerate(scenes):
                print(f"  {i + 1}. {_scene_label(s)}")
        else:
            print("\nPlaylist is empty.")
        actions = ["Add a scene"]
        if scenes:
            actions += ["Remove a scene", "Move a scene"]
        actions += ["Done"]
        action = q.select("Playlist action", choices=actions).ask()
        if action is None:
            return None
        if action == "Add a scene":
            s = _prompt_one_scene(q, audio_enabled=audio_enabled, ask_audio=False)
            if s is None:
                return None
            scenes.append(s)
        elif action == "Remove a scene":
            idx = _pick_scene_index(q, scenes, "Remove which scene?")
            if idx is None:
                return None
            scenes.pop(idx)
        elif action == "Move a scene":
            idx = _pick_scene_index(q, scenes, "Move which scene?")
            if idx is None:
                return None
            remaining = scenes[:idx] + scenes[idx + 1 :]
            pos_choices = [
                f"before {i + 1}. {_scene_label(s)}" for i, s in enumerate(remaining)
            ] + ["to the end"]
            pos = q.select("Move to which position?", choices=pos_choices).ask()
            if pos is None:
                return None
            j = pos_choices.index(pos)  # == len(remaining) -> append
            scene = scenes.pop(idx)
            scenes.insert(j, scene)
        else:  # Done
            if not scenes:
                print("Add at least one scene first.")
                continue
            return scenes


def _pick_dir(q, label: str, default_dir: str) -> str | None:  # type: ignore[no-untyped-def]
    """Pick a directory (free-text, defaulting to the loader's default dir)."""
    return q.text(
        label,
        default=default_dir,
        validate=lambda t: True if t.strip() else "required",
        instruction="  a directory of videos",
    ).ask()


def _prompt_playlist_opts(
    q,
    scenes: list[dict[str, object]],  # type: ignore[no-untyped-def]
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Ask loop / video-interleaving / interstitial options. Returns
    (playlist_overrides, interstitial_overrides) — each only the changed
    values — or None on cancel."""
    playlist: dict[str, object] = {}
    interstitial: dict[str, object] = {}

    loop = q.confirm("Loop the playlist after the last scene?", default=True).ask()
    if loop is None:
        return None
    if loop != cfgmod.PlaylistCfg().loop:
        playlist["loop"] = loop

    do_ads = q.confirm("Interleave videos between scenes?", default=False).ask()
    if do_ads is None:
        return None
    if do_ads:
        videos_dir = _pick_dir(q, "Videos directory", cfgmod.DEFAULT_VIDEO_DIR)
        if videos_dir is None:
            return None
        playlist["interleave_videos"] = True
        playlist["videos_dir"] = videos_dir.strip()

    if q.confirm("Customize the 'UP NEXT' interstitial?", default=False).ask():
        inter = _prompt_section_fields(q, "interstitial")
        if inter is None:
            return None
        interstitial.update(inter)

    return playlist, interstitial


def _write_and_offer_launch(
    q,
    cfg: cfgmod.Config,  # type: ignore[no-untyped-def]
    path_arg: str | None,
) -> tuple[str, bool] | None:
    """Shared tail: preview, confirm path, write the TOML, offer to launch.
    Returns (written_path, launch_now) or None on cancel."""
    out_path = path_arg or "c64cast.toml"
    out_path = q.text("Write to", default=out_path).ask()
    if out_path is None:
        return None
    toml = ser.dumps(cfg, schema_path=schema_directive_for(out_path))
    print("\n--- generated config -------------------------------------\n")
    print(toml)
    print("----------------------------------------------------------\n")

    if (
        os.path.exists(out_path)
        and not q.confirm(f"{out_path} exists — overwrite?", default=False).ask()
    ):
        return None
    if not q.confirm(f"Write {out_path}?", default=True).ask():
        return None
    ser.dump(cfg, out_path, schema_path=schema_directive_for(out_path))
    print(f"\n✓ wrote {out_path}")

    launch = bool(q.confirm("Launch it now?", default=False).ask())
    if not launch:
        print(f"Run it later with:  python -m c64cast --config {out_path}")
    return out_path, launch


def _run_single(q, path_arg: str | None) -> tuple[str, bool] | None:  # type: ignore[no-untyped-def]
    scene = _prompt_one_scene(q, audio_enabled=None, ask_audio=True)
    if scene is None:
        return None
    audio_overrides = _prompt_audio_backend(q, bool(scene["audio_enabled"]))
    if audio_overrides is None:
        return None
    globals_ = _prompt_globals(q)
    if globals_ is None:
        return None
    url, system = globals_

    cfg = build_config(
        scene_type=str(scene["scene_type"]),
        scene_fields=scene["scene_fields"],  # type: ignore[arg-type]
        overlays=scene["overlays"],  # type: ignore[arg-type]
        url=url,
        system=system,
        audio_enabled=scene["audio_enabled"],  # type: ignore[arg-type]
        audio_overrides=audio_overrides,
    )

    err = validate(cfg)
    if err is not None:
        print(f"\n⚠  This config doesn't validate yet:\n   {err}\n")
        if not q.confirm("Write it anyway?", default=False).ask():
            return None
    return _write_and_offer_launch(q, cfg, path_arg)


def _run_multi(q, path_arg: str | None) -> tuple[str, bool] | None:  # type: ignore[no-untyped-def]
    audio_enabled = q.confirm("Enable SID audio streaming for the playlist?", default=False).ask()
    if audio_enabled is None:
        return None

    scenes = _manage_playlist(q, audio_enabled=audio_enabled)
    if scenes is None:
        return None

    audio_overrides = _prompt_audio_backend(q, bool(audio_enabled))
    if audio_overrides is None:
        return None

    opts = _prompt_playlist_opts(q, scenes)
    if opts is None:
        return None
    playlist_overrides, interstitial_overrides = opts

    globals_ = _prompt_globals(q)
    if globals_ is None:
        return None
    url, system = globals_

    cfg = build_multi_config(
        scenes=[
            make_scene(
                str(s["scene_type"]),
                s["scene_fields"],  # type: ignore[arg-type]
                s["overlays"],  # type: ignore[arg-type]
            )
            for s in scenes
        ],
        url=url,
        system=system,
        audio_enabled=audio_enabled,
        audio_overrides=audio_overrides,
        playlist=playlist_overrides,
        interstitial=interstitial_overrides,
    )

    errs = validate_all(cfg)
    if errs:
        print("\n⚠  This playlist doesn't validate yet:")
        for e in errs:
            print(f"   {e}")
        print()
        if not q.confirm("Write it anyway?", default=False).ask():
            return None
    return _write_and_offer_launch(q, cfg, path_arg)


def run_init(path_arg: str | None) -> tuple[str, bool] | None:
    """Drive the interactive build. Returns (written_path, launch_now) on a
    successful write, or None if cancelled / dependency missing."""
    q = _ensure_questionary()
    if q is None:
        print(
            "The config wizard needs the 'wizard' extra:\n"
            "  uv sync --extra wizard   (or: pip install c64cast[wizard])"
        )
        return None

    print("c64cast config wizard.\nPress Enter to accept the [default]; Ctrl-C to cancel.\n")

    mode = q.select(
        "Build a single scene or a multi-scene playlist?", choices=[_SINGLE_LABEL, _MULTI_LABEL]
    ).ask()
    if mode is None:
        return None
    if mode == _MULTI_LABEL:
        return _run_multi(q, path_arg)
    return _run_single(q, path_arg)
