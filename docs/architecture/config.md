# Config, CLI & ensemble

How a run is assembled and coordinated: path resolution, the config loader and its precedence layers, the CLI front door, and multi-system ensemble coordination.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`ensemble.py` — audio slot coordination](#ensemblepy--audio-slot-coordination)
* [`orchestrator.py` + `orchestrators/` — cross-ensemble scene coordination](#orchestratorpy--orchestrators--cross-ensemble-scene-coordination)
* [`paths.py`](#pathspy)
* [`config.py`](#configpy)
* [`cli.py`](#clipy)
* [`recording_metadata.py` — per-scene SCENE_CONFIG_JSON logging](#recording_metadatapy--per-scene-scene_config_json-logging)

---

## `ensemble.py` — audio slot coordination

**Ensemble audio coordination.** In ensemble mode (`[ensemble]` in the master TOML) at most one system's playlist may hold the ensemble audio slot — tracked as `Ensemble.audio_holder` + `audio_lock` in [ensemble.py](../../c64cast/ensemble.py). Scenes whose class sets `WANTS_AUDIO_LOCK = True` (`VideoScene`, `WaveformScene`, `MidiScene`, `LauncherScene`) try to claim the slot in `Playlist._resolve_next_index`; on contention they get **skipped** to the next non-gated scene in the playlist, with `_safe_teardown` releasing the slot when the holder's scene ends. (`LauncherScene` overrides `competes_for_audio_lock()` so `bypass_audio_lock = true` opts out — several systems can then run interactive launchers at once, each player hearing their own SID.) Live scenes (`WebcamScene`, `BlankScene`) are built with `audio = None` in ensemble mode regardless of TOML — they never compete for the SID. Single-system runs keep `ensemble = None` and bypass the gate entirely. An ensemble system whose playlist is entirely audio-bearing scenes will idle when the slot is held elsewhere; the loader emits a WARNING on this configuration.

## `orchestrator.py` + `orchestrators/` — cross-ensemble scene coordination

The framework for a scene that spans **all** systems in an ensemble instead of running on one. When a system's playlist enters a scene with `orchestrate = true` (`SceneCfg.orchestrate`; requires `name`, ignored single-system), that system becomes the **conductor**: `resolve_orchestrator(scene_cfg)` picks the `Orchestrator` subclass that `claims()` the scene's shape, and every *other* system is interrupted and runs a **follower scene** until the conductor releases it.

Two patterns share one interface. **Span** (shipped: `BigTextSpanOrchestrator`) — each follower renders a *slice* of the conductor's content, so N screens act as one 320·N-pixel canvas. **Mirror** (not yet implemented) — each follower renders the *same* content in lockstep (synchronized video / SID / a webcam only one system is wired to); same protocol, the snapshot just carries different state.

**Registry.** `@register_orchestrator` appends to a process-wide list at import time; subclasses live in `c64cast/orchestrators/` and only register if imported, so `c64cast.cli` imports the package at startup. `resolve_orchestrator` raises `OrchestratorError` when **zero or more than one** subclass claims a scene — both are configuration bugs, and the design deliberately surfaces them at load time rather than leaving them to be debugged from a broken broadcast.

**Event plumbing lives on `Ensemble`, not the orchestrator.** `ensemble.broadcast_interrupt` / `broadcast_resume` are per-system `threading.Event` dicts (`populate_broadcast_events`), and `ensemble.active_orchestrator` is a single-slot field. This split matters: each follower playlist holds a reference to *its own* ensemble event, so a wake-up is delivered regardless of which orchestrator instance owns the current broadcast. `begin(cfg)` returns `False` (rather than raising) if a broadcast is already running, so the conductor's playlist falls back to rendering locally instead of hanging; it **clears leftover resume events before setting the interrupts**, so the new cycle's `end()` is unambiguously the one that fires them. `end()` is idempotent.

**Follower side** is `Playlist._handle_broadcast_interrupt` (driven from the run loop when `_broadcast_interrupt` is set). It saves the current scene index, tears the current scene fully down (overlays hold threads/network state, so a half-teardown leaks), builds the follower scene via the injected `build_follower_scene` factory, stamps `_orchestrator` / `_is_conductor = False` / `_system_index` onto it, spins `_run_one_frame` until `_broadcast_resume` fires or `stop_event` is set, then tears down and restores the saved index. Two guards worth knowing: a **stale** interrupt (orchestrator ended between `set` and the check) is ignored rather than fatal, and `_safe_setup` **skips installing a conductor orchestrator on a scene that already has one** — the follower's fallback cfg can *be* the conductor's `orchestrate = true` cfg, so without that check a follower would clobber its own role and become a second conductor. `_safe_teardown` clears both `ensemble.active_orchestrator` and the scene's stamp, because a scene object is reused across loop iterations and a stale `_orchestrator` would suppress the next setup.

**Follower cfg resolution.** `follower_scene_cfg_for(name)` prefers a scene in the follower's own per-system TOML with the same `name` **and `orchestrate = false`** (so per-system visual params take effect without minting a second conductor), else falls back to the conductor's cfg. `follower_only = true` marks a scene as available for this override but excluded from normal rotation.

**Subclass hooks** are non-abstract by design (`_on_begin` / `_on_end`) so a subclass opts in only when it has per-broadcast state. `_on_begin` may **raise `OrchestratorError` to refuse a broadcast**, and the exception propagates out through `begin()` so the conductor surfaces a clean error rather than starting a half-built broadcast. `snapshot()` is the one abstract state channel and is called from **follower render threads**, so it must be thread-safe.

**`orchestrators/big_text_span.py`** is the worked example. It `claims()` a `blank` or `mcm` scene carrying a `big_text` overlay, and refuses in `_on_begin` unless the conductor is the **rightmost** system — the message enters from the right edge of that screen, so any other conductor is geometrically wrong. It keeps its own `_state_lock` separate from the base's `_lock` so follower `snapshot()` reads don't contend with `begin`/`end`. One ordering subtlety drives two comments in the file: the conductor's overlay calls `publish_bits` **before** `begin()`, so followers see populated state the instant their interrupt fires — which is why `_on_begin` resets only `_abs_scroll_px` and leaves `_bits` alone, and why `_bits` is allocated in `__init__` rather than per-broadcast. `local_x_left_px(follower_index, abs_scroll_px)` is a pure function (unit-tested directly) mapping a system's left-to-right index to its window into the shared canvas; `end_threshold_px` tells the conductor when the message has cleared the leftmost screen.

## `paths.py`

The single source of truth for **where machine-local files live**, so the app works identically from a repo checkout, a `pip install`, or a PyPI wheel. The four `Path(__file__).resolve().parent.parent` repo-anchored globals it replaces — in `dac_calibration`, `transport`, and `wled_device` — were a latent bug for any non-editable install.

**Two structural rules.**

* *Stdlib-only, and it deliberately imports nothing from the package.* That puts it at the bottom of the dependency graph, so config, transport, doctor, and cli can all import it without a cycle.
* *Everything is a function, never a module constant.* Env overrides are therefore read late, on each call, so a test can redirect them with a plain `mock.patch.dict(os.environ, ...)`. Every call site is a cold path anyway — config load, calibration save, preset-store construction.

**The resolvers.**

| Call | Resolves to |
| --- | --- |
| `settings_path()` | The machine-settings TOML: `$C64CAST_SETTINGS`, else `<config base>/c64cast/settings.toml` |
| `data_root()` | The persisted-data base: `$C64CAST_DATA_DIR`, else `<data base>/c64cast` |
| `calibration_dir()`, `presets_dir()`, `loop_presets_dir()` | Derived from `data_root()` |

Config base is `%APPDATA%` on Windows, else `$XDG_CONFIG_HOME`, else `~/.config`. Data base is `%LOCALAPPDATA%`, else `$XDG_DATA_HOME`, else `~/.local/share`.

**`legacy_data_root()`** returns the old repo anchor **only** when a `pyproject.toml` sits there — that is, a source checkout rather than an installed package. It is consumed solely by `doctor._probe_data_dirs` to print `mv` migration hints. There is no implicit migration.

## `config.py`

Dataclasses for each section, `load()` parses TOML, `merge_cli()` overlays argparse values (only non-None ones — argparse defaults to None for every overridable option so the merge is unambiguous). `scenes_from_config()` is the factory that turns `[[scenes]]` entries into real Scene instances; it also handles video-interleaving from `[playlist].videos_dir`.

### Machine-settings layer

**The shared apply loop.** `_apply_toml_sections(cfg, data, *, source)` — the scalar sections, the `[color]`/`hue_corrections` special case, and the tri-state/device validations — was extracted from `load()` so the project file **and** the machine-settings file go through identical code. Same unknown-key difflib warnings, same validations.

**Loading and overlaying.** `load_machine_settings()` parses `paths.settings_path()`: a missing file yields `{}`, a parse error raises `ConfigError`, and `[[scenes]]`/`[ensemble]` are rejected with a warning and dropped — the file holds cross-run *defaults*, not playlists. `apply_machine_settings(cfg)` then overlays it onto a `cfg` in place, logging one INFO line naming the path and field count when a file was loaded.

**Where it applies.** It is the lowest layer above the dataclass defaults, applied **first** in three places:

1. `load()`, before the file's own sections.
2. The `load_master` ensemble branch — the master-defaults `Config()` gets the overlay first, so master TOML beats machine settings.
3. `quickcast.build_config`, so quick playback inherits it.

Full precedence: defaults → machine settings → project/per-system TOML → master cascade → CLI → env.

**The ensemble subtlety.** `apply_master_defaults(defaults, sys_cfg, baseline=...)` takes a **machine-overlaid** `baseline`, not a fresh blank `Config()`, as its "did this system set the field" reference. A value coming only from the machine layer therefore counts as unset, so the master TOML can still override it, while an explicit per-system value wins over both. That keeps machine < master < per-system exact.

Path resolution is [`paths.py`](#pathspy).

`_display_mode_for_scene` (shared by the webcam/video/slideshow/generative branches of `build_scene`) is also where `[color].dither = "auto"` resolves to a concrete method per scene type via `resolve_dither_method` (static `slideshow` → `floyd-steinberg`; motion scenes → `blue_noise`) before threading `dither_method`/`dither_strength` into `_build_display_mode` → the mhires/mcm/hires constructors. See the `[color].dither` note under `modes.py` above for the mechanism.

A video scene's `file` may be a single media **URL** (direct link or a yt-dlp-resolved YouTube/etc. page). `build_scene` resolves it via `quickcast.resolve_video_url` — the **one** resolution site, shared with quick playback — so configs and `c64cast MEDIA…` behave identically: the URL's `t=`/`start=`/`#t=` timestamp folds into `start_s` (an explicit `start_s` wins), the resolved title becomes the scene name, and audio-only URLs are rejected. Resolution is a network call at load/build time only — never in `validate_scene_cfg`, which stays offline. `validate_scene_cfg` (hence `--doctor`) instead does an **offline** check: a single non-direct URL (`_is_single_url_spec` + `quickcast.url_needs_ytdlp`) with the `yt` extra missing is rejected up front with an install hint, rather than failing at playback with ffmpeg's cryptic `Invalid data found` when PyAV tries to open the page as a media file.

## `cli.py`

`_resolve_configs(args)` picks the front door: positional `MEDIA` args route to `quickcast.build_config` (in-memory single-system Config; mutually exclusive with `--config`); otherwise `config.load_master` (+`merge_cli`) for the config-driven path. The scheme-aware `-u/--url` target (or `$C64CAST_URL`) is then applied via `connect.apply_to_config` onto the single system's connection fields — skipped in ensemble mode, where the per-system-flag guard rejects a CLI target so each system keeps its TOML identity. From there both paths share one run path: `config.scenes_from_config()` builds the playlist, and the Playlist gets an `interstitial_factory` built from the `[interstitial]` section and a `CommodoreKeyPoller` for pause/resume. `merge_cli` no longer touches the connection fields (the URI owns them), so a SIGHUP/control-plane reload re-merges scenes without disturbing the already-built backend.

`run_save_settings(args)` implements **`--save-settings`** — a config-free command (dispatched in `main()` right after `run_introspection`, before `_resolve_configs`, like `--init`): it starts from a machine-overlaid `Config()` (so a save *merges* with the existing file), applies this invocation's whitelisted flags (`-u/--url` decomposed via `connect.parse_connection_uri`, `-d/--device`, `-D/--audio-device`, `--sid-model`, `-s/--system`), and writes the result sparsely (`config_serialize.dumps(minimal=True, schema_path=None)` → only non-default fields) and atomically (`transport.atomic_write_text` → `paths.settings_path()`), printing the path + contents and exiting 0. Nothing savable provided → prints what's savable and exits 2. `$C64CAST_URL` never auto-saves (explicit flags only); the DMA password can never be written (`config_serialize` suppresses `_SECRET_FIELDS`). `_connection_is_builtin_default(cfg)` gates the quick-playback "no connection target" warning so it fires only when neither a CLI target nor machine settings supplied a connection.

## `recording_metadata.py` — per-scene SCENE_CONFIG_JSON logging

`Playlist._safe_setup` calls `log_scene_recording_metadata` once per scene activation (loop re-entries and random-pool re-picks included, right after `scene.setup()` so `scene.filepath`/`scene.name` are already the picked values) to log one `SCENE_CONFIG_JSON`-tagged line: a snapshot of that scene's *coalesced* settings — `scene._cfg` (the `SceneCfg` already merged defaults→config→CLI by `config.merge_cli`, filtered per scene type via the same `applies_to`-metadata idiom as `introspect.scene_types`) plus the relevant slices of the global `Config` (`[color]` in full, a curated `[audio]` subset, and `hardware.backend`/`ultimate64.system`/`ultimate64.sid_model`). `build_scene_recording_metadata` does this from already-resolved in-memory state only — no live U64/TeensyROM reads, since a REST poll during an active recording risks contending with the DMA link (see the "no rapid U64 reads during capture" practice).

Two things are deliberately left out, because the payload is meant to be pasted straight into a public YouTube description: `[ultimate64].url`/`dma_password` and `[teensyrom].host`/`serial_port` never appear (only the backend kind + system + sid_model do), and nothing resolves further than the *configured* value — e.g. `[audio].dac_curve` is logged as configured (`"auto"`, `"mahoney_ultisid"`, …), not further resolved against live calibration-file/device-identity state.

The `source` block is scene-type-specific. For `video`, `config.build_scene` (see the note above) resolves a URL's `file` into a local `file_spec` var for the `VideoScene` constructor but never mutates `s.file` itself — so `scene._cfg.file` still holds the **original URL exactly as given** with no extra plumbing, while the actual (often ugly, CDN-signed) resolved stream URL never appears in the log. `copyright` is a fixed placeholder string today; c64cast doesn't collect yt-dlp uploader/license metadata anywhere (adding it would mean changing `quickcast.resolve_media_url`/`resolve_video_url`'s tuple-return shape and its exact-equality tests — deferred). `waveform` and `generative` (with `audio_source = "sid"`) scenes are different: the PSID header (`WaveformScene.header` / `SidFileAudioSource.header`, a `sid_host_emu.SidHeader`) routinely carries a real `name`/`author`/`released` (copyright year + composer), so those are used verbatim — no placeholder.

`extract_scene_configs(log_text)` pulls every `SCENE_CONFIG_JSON` payload back out of a `--log-file` run (formatter-agnostic — it searches for the marker substring, not a fixed line format), and `render_description(payload)` renders one entry as a human, paste-ready text block; both are pure functions so [scripts/scene_config_to_description.py](../../scripts/scene_config_to_description.py) is a thin argparse+file-I/O shell around them (default: render the last entry; `--all`/`--index N` for the rest).
