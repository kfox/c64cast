# WLED bridge

Three independent bridges to the WLED ecosystem — broadcast audio-sync out, act as a WLED device, and receive a realtime pixel stream.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`wled_sync.py` — WLED audio-sync broadcast (WLED bridge Mode 3)](#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3)
* [`wled_device.py` — virtual WLED device / control surface (WLED bridge Mode 1)](#wled_devicepy--virtual-wled-device--control-surface-wled-bridge-mode-1)
* [`wled_sink.py` — virtual LED matrix / realtime pixel sink (WLED bridge Mode 2)](#wled_sinkpy--virtual-led-matrix--realtime-pixel-sink-wled-bridge-mode-2)

---

## `wled_sync.py` — WLED audio-sync broadcast (WLED bridge Mode 3)

Turns the music features c64cast already computes host-side into **WLED "Audio Sync"** UDP packets and multicasts them on the LAN, so real WLED LED matrices/strips react to whatever SID is playing — **no microphone** on the WLED side. This is the most self-contained slice of the three-mode WLED bridge (see the `project_wled_bridge` memory): it reuses the existing music stack, adds **no dependency** (pure stdlib UDP), and is orthogonal to Mode 1 (control surface — `wled_device.py`, below) & Mode 2 (realtime pixel sink — `wled_sink.py`, below).

**Wire format.** WLED's `audioreactive` usermod (Sound Sync = "Receive") listens on multicast **239.0.0.1:11988**. The V2 `audioSyncPacket` is 44 bytes, `__attribute__((packed))`, little-endian — `struct` format `"<6s2xffBx16B2xff"` (header `"00002\0"`, two reserved gap bytes, `sampleRaw`/`sampleSmth` floats, `samplePeak` byte, a gap, the 16-byte GEQ `fftResult`, a gap, `FFT_Magnitude`, `FFT_MajorPeak`). **Verified against WLED source** (`usermods/audioreactive/audio_reactive.cpp`) at implementation time — the original plan-memory field list was wrong (it listed `pressure`/`zeroCrossingCount` from V1; the real V2 carries compiler `reserved` gaps instead). `struct.calcsize` asserts 44 at import so a format typo can't ship. Re-verify against firmware if WLED bumps the header past `"00002"`.

**Feature source.** The broadcaster is fed a `features_fn` returning a `MusicModulation | None`; the `Playlist` wires it to `self.current.features()` (a new base-`Scene` method, default `None`). `WaveformScene` overrides it from its scope's primary `SIDEmulator` (level/per-voice freq+gate), and the `generative`+`audio_source="sid"` `SourceScene` delegates to its `SidFileAudioSource.features()` (the existing `SidFeatureStream`). Every other scene returns `None` → nothing is sent. The broadcaster runs on its own `PollThread` at `[wled].rate_hz` (default 50 Hz), decoupled from render fps.

**Tempo fallback (Live DJ/VJ Phase 6).** With `[wled].broadcast_tempo_fallback = true`, `Playlist._active_features` fills a `None` (a non-SID scene — video/webcam/slideshow, or the brief gap between scenes) from the **beat grid** instead: it returns `self._clock_modulation.features()` (the `tempo.ClockModulationSource` that already wraps `pl.tempo` for effect tempo-lock) while `pl.tempo.running`. So real WLED strips keep pulsing to the MIDI/tap tempo on non-SID scenes instead of going dark — the same `ClockModulationSource` snapshot (a per-beat onset envelope + bpm), fed into the same 44-byte packet path. A SID-driven scene always wins (the fallback only fills a `None`); off by default, so pre-Phase-6 runs are unchanged (`validate_wled_cfg` skips the "no SID scene" warning when the fallback is on, since the grid then supplies packets). It's opt-in because the internal grid free-runs `running=True` at a static BPM, which would otherwise pulse every non-SID scene unbidden.

**Mapping** (`build_audio_sync_packet`, pure/unit-tested): `level` (0..1) → `sampleRaw`=`sampleSmth`=`level*255` (WLED volume range); each *gated, nonzero-freq* voice lights the GEQ bin its frequency maps to (`_freq_to_geq_bin`, log-spaced 40 Hz…10 kHz, clamped 0..15) at `level*255`; `FFT_MajorPeak` = the highest-frequency active voice (we have no per-voice amplitude to rank the true dominant partial), clamped to WLED's 1..11025 Hz; `FFT_Magnitude` = `level*255`.

**Peak derivation lives in the broadcaster, not the sources.** `samplePeak` — the transient flag most WLED audio effects key off — is set when the source reports `onset > 0.5` **OR** any voice gate rose since the previous packet (`_prev_gates`). This is deliberate: `WaveformScene.features()` reports `onset=0` (the scope doesn't track onsets), so without the broadcaster's own gate-edge detection it would never flash. Sampling gates at the broadcast rate (50 Hz) catches virtually every note-on since SID notes span multiple frames.

### Lifecycle + config
 Built in `Playlist.__init__` only when `config.resolve_wled_broadcast` reports on. The `[wled].broadcast` field combines on/off + endpoint in one string (`disabled` | `enabled` | `[host][:port]`); `parse_wled_endpoint` decodes it, with `enabled` → the multicast group (zero-config reaches every receiving WLED on the segment) and a `[host][:port]` targeting one device unicast. `start()` opens the UDP socket (multicast TTL 1) and the emit thread just before the run loop; `stop()` in the run loop's `finally`. Send errors are counted + logged once, never propagated — a broadcast hiccup must not disturb playback. `config.validate_wled_cfg` bounds port/rate and warns (doesn't fail) when enabled with no SID-driven scene to source from; `doctor._validate_wled` reports the resolved target per system. **Follow-ups** (in the memory): ensemble fan-out (per-system `[wled]` would each multicast — needs coordination), and using `python-wled` to *set* effects/palette on the matrices to complement the audio sync.

## `wled_device.py` — virtual WLED device / control surface (WLED bridge Mode 1)

The other direction of the bridge: where Mode 3 pushes SID features *out*, Mode 1 lets the **WLED ecosystem control c64cast**. c64cast advertises itself as a WLED device over mDNS (`_wled._tcp`, via `zeroconf`) and serves a subset of WLED's **JSON HTTP + WS API**, so the WLED mobile app, `python-wled`, or Home Assistant discover and drive it with no c64cast-specific client. Gated on the **`wled` extra** (zeroconf + fastapi + uvicorn); Mode 3 stays pure-stdlib and independent.

**No `from __future__ import annotations` in this module — on purpose.** The FastAPI route handlers annotate params with types imported *inside* `build_wled_app` (`Request`, `WebSocket`). With stringized annotations FastAPI resolves them via `get_type_hints` against **module globals**, where those locally-imported names don't exist — so it mis-reads `request` as a query param (422s every POST) and never injects the `WebSocket`. Eager (real) annotations resolve against the enclosing function scope, which is what FastAPI needs. (`control_plane.py` sidesteps the same trap differently — it only annotates params with builtins + `Query` defaults.)

### `WledBridge` — the translation layer

Holds an ordered `list[(name, Playlist)]` with one **WLED segment per system**, in ensemble order; a single-system run is one segment.

#### Reads — building the WLED JSON

* **`state_dict`** — per segment: `on` = not-paused, `fx` = `Playlist.index`, plus echoed `bri`/`pal`/`col`/`sx`/`ix`, and a `c64` vendor key of per-control applicability booleans (see `_seg_caps` below).
* **`info_dict`** — identifies as a WLED build (`ver`, `brand`, `product`, `leds.count` = segment count) so clients parse us.
* **`effects`** — the **shared** WLED effect list, taken from system[0]'s scene `wled_label`s. Normally that is the scene name, but a randomized-asset scene reports a stable pool label so the dropdown does not churn as the asset rotates. WLED has one global effect array, and a segment's `fx` indexes into it, clamped to that system's own scene count.
* **`palettes`** — the c64cast **palette modes** (`modes.PALETTE_MODES`, title-cased, as `_WLED_PALETTES`). Index-stable, so a `pal` index maps back to a mode.

**Why `vid` is content-derived.** `_content_vid` returns `_WLED_VID_BASE + hash(effect+palette names)`. The WLED app and UI cache the effect and palette lists keyed on `(vid, palcount)`, re-fetching only when one changes — so a *fixed* `vid` leaves a **stale scene dropdown** after the playlist changes. Hashing the names bumps `vid` whenever the scene or palette set does. It is kept ≥ base and date-int-shaped so version gates pass, and `ver` is never touched so the upgrade nag stays quiet.

#### Writes — `apply(partial)`

Top-level `on` gates **every** system's transport (a master switch) and top-level `bri` is a master dim. A `seg[]` list targets systems by id or position.

* **`on`** → `pause_event` / `resume_event`. A segment plays when master-on **and** its own on. `bri` only dims and is decoupled from transport — see `_apply_dim`.
* **`fx`** → `request_jump(fx, skip_interstitial=True)`. Skipped when `fx` is already the current scene, so a redundant re-select or a same-scene preset recall never restarts it.
* **`sx` / `ix`** → the current scene's first-declared `LIVE_PARAMS` among `_SX_TARGETS` / `_IX_TARGETS`, via `_set_live_param` — the same holder/`LIVE_PARAMS` seam `midi_control._apply_param` uses. A no-op when the scene declares none.

`_IX_TARGETS = ("source.scale", "source.intensity", "effect.intensity", "scene.gain")`. Source-first preserves generator behavior; `effect.intensity` reaches the pulse/rgb_shift reaction depth; and the `scene.gain` fallback drives the scope scenes, which have no source or effect holder — see the `scene.` prefix in the `LIVE_PARAMS` registry note.

`sx` stays a documented no-op on scope scenes: their only speed-ish knob, `auto_cycles`, is dead under the default `time_base="wallclock"`.

#### `pal` and `col` — both functional

Module helpers `_apply_palette` and `_apply_force_colors`, both routed through `_current_mode_api(pl)`.

**`pal`** → `DisplayMode.set_palette_mode(api, PALETTE_MODES[i], force_palette=False)` on the current scene's mode — the same live seam the on-C64 menu and SHIFT cycle use, updating state and calling `invalidate_cache` with no socket write. It also **clears any active color force** (`set_color_map(None)`), which is the intuitive "back to normal palette" path.

**`col`** snaps its up-to-3 `[R,G,B]` slots to nearest C64 indices via `palette.nearest_palette_index` (perceptual/Lab), builds a source-scan-free `palette.build_fixed_color_map`, and installs it live with the mode's force toggle on. It snaps the mode to `percell` — the invariant a forced palette pairs with — so a color pick shows even when grayscale was active. The scene then **posterizes to the picked colors**; a single color gets a black or white partner for contrast.

**The only-when-changed guard.** `pal` and `col` map onto the *same* C64 palette, so they are mutually exclusive intents. But the WLED app re-POSTs the **full** segment, both fields, on every change. So each is applied only when it *changed* from the last-echoed value — otherwise an unchanged `col` riding along a palette pick would re-force and clobber it. This was a hardware-surfaced bug.

Both are silent echo-only no-ops on modes without `set_palette_mode` / `set_color_map` — hires, petscii, blank. Only MCM and MultiHires apply a forced palette.

#### Dispatch and thread safety

Brightness is a **real output dim** (`_apply_dim`, under Lifecycle below).

`apply` dispatches the WLED preset verbs (`psave`/`pdel`/`ps`) first, and otherwise delegates to `_apply_locked(partial, *, force_palcol=False)` — see the presets note below.

Transport is event-based and the preset/echo state is lock-guarded, so `apply` is safe from both the uvicorn threadpool (POST) and the event loop (WS). The numpy/cv2 ColorMap bake runs on the infrequent, user-driven WLED thread before the atomic attribute swap.

### The app + server
`build_wled_app` exposes:

* **GET** `/json` (state + info + effects + palettes), `/json/state`, `/json/info`, `/json/si` (state+info, WLED's light poll), `/json/eff`, `/json/pal`.
* **POST** `/json` and `/json/state` — apply, returning `{"success": true}`.
* **WS** `/ws`.

**The WS handler** sends `{state, info}` on connect, then loops on `asyncio.wait_for(receive_json, timeout=_WS_PUSH_INTERVAL_S)`. On a message it applies and broadcasts; **on timeout it pushes state to the client if it changed on its own** — a playlist auto-advance, or a queued jump landing.

That proactive push matters because real WLED pushes state proactively. Without it, nothing else drives our WS, so the app's **Scene field goes stale** between the user's own actions. Hardware testing surfaced exactly this.

**The self-served `GET /` control page** (`_INDEX_HTML`, a dependency-free fetch-driven page for third-party WebView shell apps and any browser) mirrors exactly the functional controls: power, and per-system scene select, **palette select**, a per-segment **brightness slider**, speed/intensity sliders, and a **color picker**. The DOM is built client-side from `/json`, and each segment is titled by its `seg[].n` — the system name, with a single-system run using the configurable `[wled].name`, so it is never a bare "System 1".

The brightness slider was once removed as a dead power-duplicate. It is back now that `bri` is a **real screen dim** (`_apply_dim` → `user_dim`, see the [`modes.py`](video-color.md#modespy--displaymode-hierarchy) note) rather than only an echo: `bri=0` still means off (pause), and any nonzero value darkens the C64 output.

**Serving and discovery.** `WledDeviceServer` runs the app on `control_plane.ControlServer` — the shared uvicorn-on-a-background-thread wrapper, now taking a `label` so its log line reads "WLED device …" rather than "control plane". It registers and unregisters a `ServiceInfo` for `_wled._tcp.local.`, advertising the real LAN IP via `_local_ip` (a UDP-connect trick) even when bound to `0.0.0.0`; the SRV record carries the actual port, so discovery works on a non-privileged bind. An mDNS registration failure is logged but never takes down the already-serving HTTP API.

### Lifecycle + config
**Startup.** `cli.main` starts it after the control plane and before the run loop, when `config.resolve_wled_listen(cfgs[0])` reports on, spanning **all** systems (`systems=[(st.name, st.playlist) …]`). The first system's `[wled].listen` governs the cluster device. It is stopped in the `finally` alongside the control plane.

`[wled].listen` uses the same combined-string grammar as `broadcast` (`enabled` → `0.0.0.0:8080`), and `[wled].name` is the advertised friendly name. A missing extra raises a graceful `RuntimeError` naming the missing piece, mirroring the control-plane pattern; `doctor._validate_wled` reports the resolved listen bind.

#### `bri` → a real dim, decoupled from transport

`WledBridge._apply_dim` maps `bri` to the effective brightness `(master/255)*(seg/255)` and pushes it onto `Playlist.user_dim` and the live mode's `user_dim` (see the `modes.py` fade note). A top-level `bri` change re-dims every system.

Brightness is **independent of transport**: pause/resume is the Power (`on`) toggle alone, and `bri=0` dims fully to black (`user_dim=0`) but does *not* pause.

> This decoupling fixed a hardware bug. `bri=0` used to pause — and `_handle_pause` → `api.pause_idle()` resets the Ultimate to the BASIC READY banner with a flashing cursor. So nudging the brightness slider through 0 mid-transition reset the machine.

The self-served `/` page exposes brightness as a single **master** slider driving top-level `bri` — the same field the WLED app's native brightness drives, so the two stay in sync — rather than per-segment.

#### `_seg_caps` — per-control capability hints

`_seg_dict` rides a `c64` vendor key of `{pal, col, sx, ix}` booleans, computed by `_seg_caps(pl)` to mirror exactly the write-path applicability guards:

| Key | True when |
| --- | --- |
| `pal` | The current scene's mode exposes `set_palette_mode` (what `_apply_palette` needs) |
| `col` | It *also* exposes `set_color_map` (what `_apply_force_colors` needs) |
| `sx` / `ix` | `_resolve_live_target` — extracted from `_set_live_param`, including the `scene.` case — finds a matching `LIVE_PARAM` on `_SX_TARGETS`/`_IX_TARGETS` |

No scene means all False.

Our own `GET /` page reads it as `seg.c64`, defaulting all-true so an older payload never over-disables, and grays out the palette select, color picker, and speed/intensity sliders when their bool is false — disabling them, adding a dimmed `.cap-off` class, and a tooltip. Scene, Power, and Brightness always apply and are never gated.

The key rides both the `/json` poll and the proactive WS push, so hints refresh on auto-advance for free. WLED clients ignore the unknown seg key, though they must still *parse* the payload — that was the load-bearing hardware check.

It does **not** touch `midi_control`; that mirror of `_set_live_param` stays verbatim.

> The third-party WLED *app* still renders a fixed control set we cannot remotely disable, so a dead palette or color pick there remains a silent no-op. Only the `/` page reflects capability.

### Presets — `PresetStore` + client-sequenced recall
WLED "presets" capture the current look and recall it in one tap.

**Dispatch.** `apply(partial)` handles the WLED preset verbs before any state application:

* `psave` / `n` — snapshot the current transport plus per-segment echo into a preset dict and store it, auto-picking the next free id when `psave<1`.
* `pdel` — delete.
* `ps` — recall.
* Anything else is a genuine manual change: it resets `_active_preset` to −1 and delegates to the extracted `_apply_locked(partial, *, force_palcol=False)`.

Recall calls `_apply_locked(preset, force_palcol=True)` so the stored palette and color re-apply **past** the only-when-changed echo guard. That guard exists to stop the app's incidental full-segment re-POSTs, not deliberate recalls.

`state.ps` reports `_active_preset`, and `info.fs.pmt` is the presets-file mtime in ms so clients can cache and re-fetch `/presets.json`.

**Storage.** `PresetStore` keeps one JSON file per device name at `paths.presets_dir()`/`wled-<slug>.json` — the canonical `<data root>/presets/`, `$C64CAST_DATA_DIR`-overridable and resolved at use time (see [`paths.py`](config.md#pathspy); gitignored at the legacy repo location, with only the README tracked). It holds the WLED preset map `{"1": {...}}`, ids 1–250, with id 0 reserved empty.

Loads are tolerant — missing or corrupt yields empty. Writes are atomic: a temp file in the same dir, `fsync`, then `os.replace`. So it survives restarts like real WLED. The path is injectable so tests can point it at a tempdir.

**No server-side timing or daemon thread.** The deferral is client-managed, and the two clients differ:

* **The third-party app** sends `{ps:N}` with no orchestration, so recall is immediate best-effort. Same-scene recall is perfect; cross-scene sliders, `pal`, and `col` land on the *outgoing* scene and may miss, since the jump has not settled.
* **The `/` page** sequences it client-side over WS. On Apply it POSTs `{ps:N}` and remembers each segment's target `fx`; once a WS state push shows that `fx` live, it re-fires `{ps:N}`. `fx` now matches, so `_apply_to_system` skips the guarded re-jump and simply re-applies the sliders and forces palette/color onto the now-live scene.

That WS sequencing is why the `/` page's live feed moved from a 4 s poll to the `/ws` socket. A scene jump to the already-current scene is also now skipped in `_apply_to_system`, so recall never needlessly restarts a scene.

**Randomization-aware naming.** The default preset name (when the client sends no `n`) and the WLED effect list both use `Scene.wled_label`, which defaults to `self.name`. `WaveformScene` overrides it to a stable `"SID: random pool"` for a multi-entry pool (`len(_candidates) > 1`).

The reason: its `self.name` is the currently-loaded tune and rotates each `setup()`. Naming a preset — or the effect dropdown — after it would falsely promise one tune and churn `vid`. A single fixed tune keeps its real title. `self.name` itself, used for the interstitial "up next" and the on-screen title row, is untouched.

> **Follow-ups:** the queued-jump latency (an `fx` change lands at the next scene boundary, not instantly — ≈16–40 ms to the next rendered frame, with residual felt lag being scene teardown plus `_safe_setup`), and ensemble-as-one-tiled-matrix (per-system segments already lay the groundwork).

Effects, generators, and scope scenes all answer sx/ix — see the `LIVE_PARAMS` registry note.

## `wled_sink.py` — virtual LED matrix / realtime pixel sink (WLED bridge Mode 2)

The literal "C64 as a WLED matrix": the C64 becomes a **network pixel sink** that any WLED-ecosystem realtime sender (LedFx / xLights / another WLED syncing) streams frames to. Unlike Modes 1 & 3 this needs **no dependency** (pure stdlib `socket`/`struct`/`select`) and is *clean* because it plugs straight into the existing composable-scene seam: a **`WLEDSource(BaseFrameSource)`** (frame_source.py) hands the received frame to a `SourceScene`, and the ordinary display-mode pipeline (palette / dither / color_match) quantizes it to the C64 exactly like a webcam or a generator. Physical WLED pixel-count limits don't apply to a virtual sink — the matrix is whatever `sink_width`×`sink_height` the scene declares (default 320×200).

**Two protocols, auto-detected, on their standard ports.** The receiver binds both simultaneously and dispatches each datagram by which socket it arrived on:
- **DDP** (UDP **4048**) — what LedFx/xLights emit. `parse_ddp` decodes the 10-byte header (version `0x40`, a big-endian byte-offset + length so a frame larger than one datagram spans several packets, and the **push** flag on the final packet meaning "display now"); query/reply/discovery packets are rejected (no pixels).
- **WLED realtime UDP** (UDP **21324**) — WLED's own protocol. `parse_wled_realtime` decodes byte 0's sub-format: **WARLS** (indexed `[i,r,g,b]`), **DRGB** (from pixel 0), **DRGBW** (RGB+white, white dropped), **DNRGB** (16-bit start index, for >256 px). Byte 1 (return-to-normal timeout) is ignored — the scene owns the display lifetime.

Both parsers are pure (no I/O) so they unit-test against the documented byte layouts. `PixelFrameAssembler` holds a `width*height*3` **RGB** byte buffer — DDP writes byte-runs at absolute offsets, WLED-realtime writes `(index,r,g,b)` — both clipped to the buffer so a sender configured larger than the sink can't overflow it; `snapshot_bgr` reshapes to `(H,W,3)` and swaps RGB→BGR for cv2. **`WledPixelReceiver`** is a daemon thread (`PollThread(manual=True)`) that `select`s on both sockets and publishes the assembled frame under a lock: for DDP on the push flag (falling back to every packet if a sender never pushes), for WLED-realtime after each datagram. A bind failure (port already in use) is stored in `bind_error`; **`WLEDSource.setup`** surfaces it by setting `finished`, so the scene self-aborts and the playlist advances — the same self-abort contract a failed audio source uses. `read` returns the latest frame or `None` until the first arrives (the scene skips the render on `None`, so an idle sink simply shows nothing then holds the last frame).

**Config + wiring.** A new **scene type `wled`** (config.py `SCENE_TYPES`, `_validate_wled`, the `build_scene` branch) builds `SourceScene(api, None, mode, WLEDSource(w,h), NullAudioSource(), name)` — no audio, no SID. `display` defaults to `mhires` (arbitrary color content, like video) and rejects `blank`/`random`; `sink_width`/`sink_height` are per-scene fields (1..1024) that must match the sender's configured pixel layout. Bitmap displays get the usual half-rate `target_fps` default. **No sender on hand?** `scripts/diags/wled_pixel_sender.py` streams an animated test pattern in either protocol for HW verification (the WLED phone app is a *controller* and can't emit pixels). **Follow-ups:** E1.31/sACN (multi-universe reassembly — LedFx/xLights already speak DDP, so it buys little), and ensemble-as-one-tiled-matrix (slice the incoming frame per system by layout position).
