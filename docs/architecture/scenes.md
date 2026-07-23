# Scenes, sources & overlays

What runs on screen and for how long: the Scene state machine, the composable source/effect/audio stack, overlays, and the interstitial.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`scenes.py` — Scene state machine](#scenespy--scene-state-machine)
* [Composable scenes — `scenes.SourceScene` + `frame_source.py` + `generators.py` + `effects.py` + `audio_source.py` + `modulation.py` + `music_features.py`](#composable-scenes--scenessourcescene--frame_sourcepy--generatorspy--effectspy--audio_sourcepy--modulationpy--music_featurespy)
* [`overlays/`](#overlays)
* [`interstitial.py` + `backgrounds.py`](#interstitialpy--backgroundspy)

---

## `scenes.py` — Scene state machine

The Playlist calls `setup()` → `process_frame()` (repeatedly) → `teardown()` for each scene, with an interstitial scene — built by an injected `interstitial_factory` — between them.

### `WebcamScene` — tuned for latency

Each camera frame is pushed straight through, with no delay buffer.

**Duration is context-dependent**, resolved in `config.build_scene`. Webcam and blank scenes with an unset `duration_s` default to `math.inf` ("leave the camera running") **only when they are the sole scene** in a single-scene playlist. In a multi-scene playlist they keep the base 30 s, because an infinite live scene never becomes `is_done` and would wedge the rotation.

An explicit `duration_s = 0` is the universal "run forever" sentinel, mapped to `math.inf` for any non-video type. A negative value is rejected at load.

**Audio** follows the global `[audio].enabled` flag: when on, the webcam/blank scene picks up the AudioStreamer automatically, and a per-scene `audio = false` opts back out — useful for muting one segment of an otherwise audible playlist. When attached it runs uncorrelated to the video; sync between mic and display is not preserved.

**Backpressure** is handled at the Playlist layer via deadline-based frame-dropping, not a per-scene queue check. The DMA socket's TCP send buffer absorbs short bursts, and missed frames are dropped at the deadline rather than bursting to catch up.

### `VideoScene` — the opposite tradeoff

It uses the audio playback position as the master clock and picks the closest video frame against it, so A/V never drift.

Its lifetime is video-driven: `process_frame` returns False once the source reports `finished`, and `__init__` pins `self.duration_s = math.inf` so the base-class duration timer cannot truncate playback.

`config.validate_scene_cfg` rejects any user-supplied `duration_s` on a video cfg, since the field would be either a silent no-op or a truncation footgun. `Scene.setup` formats the infinity as `duration=video-driven` in the startup log.

Each Scene also carries an `overlays: list[Overlay]`. The Playlist runs every overlay's `setup`/`process_frame`/`teardown` around the scene's lifecycle — scene paints first, overlays paint on top (in declaration order).

### `VideoScene`'s transport surface (MIDI live-tune Phase 2)

DJ-style seek, pause, and loop, driven by `transport.TransportSession` via `[midi_control]`'s `transport.*` actions.

**The untouched clock.** `_clock_s()` normally reads the audio-position clock, or a plain wall clock from `_start_time` when unmuted with no streamer. Unchanged from before this phase.

**First touch.** Any of `transport_pause`/`_seek`/`_loop_toggle`/`_record`/`_stop`/`_loop_slot`, or a jog/rw/ff, routes through `_touch_transport()`. It:

1. **Reads the current clock BEFORE flipping `_transport_touched`.** The order is load-bearing — `_clock_s()` branches on that flag, so seeding the anchor from a post-flip read would capture the anchor's own not-yet-seeded default rather than the real pre-touch position. `VideoSceneClockTest` caught exactly this bug during Phase 2.
2. Seeds `_wall_anchor_clock_s` / `_wall_anchor_time` from that reading.
3. Calls `source.set_muted(True)` — idempotent, a no-op on later calls.

From then on `_clock_s()` free-runs from the anchor as `_wall_anchor_clock_s + (time.time() - _wall_anchor_time)`, frozen at the anchor while `_paused`.

**`transport_seek(target_s)`** clamps to `[0, duration_s or target_s]`, re-anchors the clock directly to `target_s`, and calls `source.request_seek(target_s)`. There is no separate offset bookkeeping: the wall clock **is** the file position once touched.

**`transport_loop_toggle()`** is a minimal 3-state cycle — mark A, mark B and start looping, clear — read by `process_frame`. Two things change there:

* The EOF check becomes `source.finished and loop_state != "active"`, since an active loop is never "done".
* After computing `clock_s`, a loop-active scene checks `clock_s >= loop_b or source.finished` and seeks back to `loop_a` instead of rendering that tick.

**Reset.** All transport state resets at the top of `setup()`, so a repeated or looped scene starts back on the audio-master clock, untouched.

**Debug label.** `show_frame_numbers` stops adding `start_s` once touched (`file_s = clock_s if _transport_touched else clock_s + start_s`) — past that point `clock_s` is already an absolute file position, so adding `start_s` again would double-count.

**Why REU-pump audio is force-disabled.** Whenever `[midi_control].cc_map` maps any `transport.*` action, `cli._coerce_reu_for_transport` disables it process-wide, mirroring `_coerce_reu_for_backend`'s shape and running before the shared `AudioStreamer` is constructed (since `use_reu_pump` is a constructor arg baked in there).

The reason: the REU-pump path pre-decodes the whole soundtrack onto its own C64-side clock and has no notion of splicing to a new position, so it would keep playing on its own timeline while the video jumps around.

`[video].use_reu_staged` — the REU bank-swap *bitmap* push — is untouched, since it only affects how the current frame reaches C64 memory, not which frame is current.

### Audio resync once touched (MIDI live-tune Phase 4)

Controlled by `[midi_control].loop_audio`, default `"on"`.

The Phase-2 mute-and-wall-clock described above is now the `loop_audio = "mute"` path. The default `"on"` keeps audio playing and re-syncs it across every splice.

**Choosing the path.** `_touch_transport()` resolves:

```
_transport_resync = (loop_audio == "on"
                     and audio present
                     and source.a_stream present
                     and not use_reu_pump)
```

On the resync path it does **not** mute, and seeds an **audio-anchored clock** — `_audio_anchor_clock_s` plus `_audio_anchor_pos = audio.position_seconds()` — instead of the wall anchor. `_clock_s()` then returns `anchor_clock + (audio.position_seconds() − anchor_pos)`, frozen at the anchor while paused.

**Why audio-anchored and not wall.** On DAC+bitmap the drain runs ≈0.88× wall, courtesy of the `tempo_scale` machinery, so a wall master would desync ≈7 s/min. The audio delta instead inherits exactly the shipped pre-touch clock's drift behavior on every backend.

**The splice primitive.** `_splice(target_s)` re-anchors the clock, then:

1. `source.request_seek(target_s)` — engaging `_emit_audio`'s pending-seek guard **first**.
2. `audio.flush()` — dropping the queue; the flush epoch handles a pusher already blocked inside `push_samples`.

It is used by `transport_seek`, the loop wrap, and resume-from-pause.

**Pause and resume.**

* *Pause* — freeze the anchor, `source.set_muted(True)`, `audio.flush(silence_output=True)` for a fast mute.
* *Resume* — `_splice()` back to the paused position **then** `set_muted(False)`. Splice-first is what closes the resume leak window; the sampler's plain flush also restores its channel volume.

**Loop-wrap re-fire guard.** The wrap adds `not (resync and source.seek_pending)`, so a `source.finished` wrap flushes and seeks A exactly once — not every frame until the demux clears `_eof`.

**The `tempo_scale` domain seam.** This is the bug hotspot. The internal clock stays in the scaled/PTS domain, while the transport *surface* — seek targets, loop A/B, OSD, `transport_position`, the frame-number label — speaks content seconds. `_clock_to_content` / `_content_to_clock` bridge them.

They are the identity when `tempo_scale == 1.0`, i.e. sampler, DAC+char, and muted. Those common paths therefore carry zero risk; only DAC+bitmap resync actually scales.

The `"mute"` path is preserved bit-for-bit — wall anchor, identity conversions — including its pre-existing DAC+bitmap tempo quirk, documented in [caveats.md](../caveats.md).

### Record workflow + loop preset pads (MIDI live-tune Phase 3)

`transport_record` / `transport_stop` / `transport_loop_slot`.

All three drive the **same** `_loop_a`/`_loop_b`/`_loop_state` machine that `transport_loop_toggle` already uses. Record and Stop are a second, richer entry point — not a parallel state.

**`transport_record()`** — if `_loop_state == "none"`, marks A (state `"armed"`) and turns the border red via `_set_record_border(True)`. If a loop is already armed or active, it is a no-op transition beyond the usual touch.

**`transport_stop() -> bool`** — a real 3-way state machine:

| Condition | Action | Returns |
| --- | --- | --- |
| `_loop_state == "armed"` | Mark B (`"active"`), clear the border | `False` |
| not paused | Pause | `False` |
| already paused | — | **`True`** |

On a `True` return the caller, `TransportSession._dispatch`, sets `Playlist.stop_event`. That drives a normal exit through `cli.main`'s existing `finally` — teardown, then `_maybe_save_live_tune`'s save/prompt flow. This is the "double-Stop exits" behavior.

**`transport_loop_slot(slot, *, save, clear)`** — the pad action:

* `clear` deletes the slot from `self._loop_store`. A pure file op, with no transport touch.
* `save` persists the **current** `_loop_a`/`_loop_b` into the slot, showing OSD `"NO LOOP"` when `_loop_a is None` and there is nothing to save.
* A plain press **recalls** the slot, or the whole-file default (`a=0, b=None`) if empty, sets `_loop_state = "active"`, resumes if paused, and seeks to A.

This is a deliberately **explicit-save** design, confirmed with the user over an implicit "auto-store the last recording" heuristic: a performer shouldn't have to remember per-pad context to know whether the next press saves or recalls.

**`_set_record_border(active)`** writes `api.write_regs("d020", 2 if active else 0)`, where C64 palette index 2 is red. Restoring to `0` is always correct, because the bitmap and char display modes `VideoScene` uses engage with a hardcoded `$00` border and never rewrite `$D020` per frame afterward — see `modes.engage_bitmap_mode`'s docstring, where border is a one-time engage-time poke on every mode `VideoScene` can use.

`teardown()` calls `_set_record_border(False)` unconditionally and idempotently, so an interrupted recording never leaves a red border lingering into the next scene.

**`self._loop_store`** (`transport.LoopPresetStore | None`) is rebuilt in `setup()` from `transport.make_loop_preset_store(self.filepath)` on **every** call, not just `__init__` — a directory- or glob-backed scene picks a different file each loop iteration.

### Scene fade timeline (Playlist)

When `[playlist].fade_duration_s > 0`, the Playlist drives the per-mode fade described in the `modes.py` note above. **Fade-in** overlaps the opening live frames: `_begin_fade_in` (in `_safe_setup`) sets the mode's `fade_alpha = 0`, and `_advance_fade_in` (top of `_run_one_frame`) ramps it 0→1 over `_fade_frames(scene)` frames (`round(fade_duration_s / frame_time)`), so the scene plays from frame 0, rising out of black. **Fade-out** is freeze+dim: on a *normal* scene end (not skip), `_fade_out` runs just before `_safe_teardown` in `_advance`, re-pushing the cached last frame (`repush_faded`) at descending alpha for the same span. **CTRL skip aborts both immediately** — the skip branch in `_run_one_frame` calls `_cancel_fade_in` (snap to full brightness) and sets `_ended_via_skip` (suppresses the fade-out); a skip *during* a fade-out breaks the loop and consumes the event so it doesn't also skip the next scene. The fade applies to every compose-based scene including the interstitial (each rises from and sinks to black); non-compose scenes and the reload/broadcast teardown paths are unaffected.

Each Scene also has an optional `target_fps` attribute. When set, the Playlist honors it for the duration of the scene. If unset, most scenes use the playlist's system default (60 NTSC / 50 PAL), but several default lower to stay under the DMA bus-halt ceiling (each still overridable by an explicit CLI/TOML `target_fps`):

* **Bitmap (hires/mhires) frame-pushing scenes** — `video`, live `webcam`, and `generative` with `audio_source = "mic"` — default to **20 fps** (both NTSC and PAL) while streaming the 4-bit `$D418` digitized-audio DAC, and to **half** the system rate (30 NTSC / 25 PAL) when muted. A bitmap mode re-uploads a full ≈9-10 KB frame every frame; when the audio worker is also writing the ring, the two write streams compete for the bus and the picture tears at full rate. **Exception — video on the off-bus Ultimate Audio sampler:** that audio doesn't touch the bus and forces the bus-clean REU-staged video path, so neither cap applies — it uncaps to the full system rate (60/50), which dedup turns into source-rate playback (see the `sampler.py` "fps" note). Char modes (petscii/blank) push a cheap delta-cached 1 KB screen, so they keep the system default. Resolved by `config._frame_push_default_fps`; revisit the DAC/muted caps once firmware stops halting the CPU on DMA writes.
* **`WaveformScene` / `MidiScene`** default to **half** the system rate (30 NTSC / 25 PAL). For waveform this is because 60 fps powers off the U64 on bank-2-relocated SIDs (≈170 writes/s into `$A000-$BFFF` — a suspected firmware badline/DMA bug). The host-emu / envelope poll thread stays at the full video rate regardless.

### `SlideshowScene`

Cycles through still images for the scene's `duration_s`. File spec mirrors VideoScene's grammar (comma-separated paths / dirs / globs via `resolve_file_spec`, default dir `assets/pictures/`). Per-image timer is `image_duration_s` (default 5s) — independent of `duration_s`, which controls total runtime. Picker is shuffle-and-walk: every image in the pool plays once before any repeats, and the first pick after a reshuffle is swapped with the second when the pool has >1 entry, so no image appears twice back-to-back across reshuffle boundaries. No audio, no `WANTS_AUDIO_LOCK`, no CLAHE/temporal-EMA smoothing (the webcam blend would cross-fade unrelated stills). `display = "random"` resolves to a fresh mode in `SLIDESHOW_RANDOM_DISPLAYS` at every `setup()` (so single-scene loops vary per iteration); unset `display` or the explicit value `"hires_edges"` (`_resolve_slideshow_display`, config.py) is substituted with `mhires` — `hires_edges` is tuned for live webcam Canny edges, not photos (use `display = "hires"` for plain bitmap). Video scenes get their own per-type default: unset resolves to `mhires` too (`resolve_scene_display`), but an *explicit* `display = "hires_edges"` is honored as-is (unlike slideshow's substitution) — see `_display_mode_for_scene`. Images load via `cv2.imread` (no extra dependency). `aspect_mode` (default `"crop"`) selects how each image is fit to the C64 aspect before the mode downscales it, via the shared `_apply_aspect` dispatcher: `"crop"` center-crops to fill (`_crop_to_aspect` — edges lost; the universal default), `"fit"` letterbox/pillarboxes with `_fit_to_aspect` so the whole image shows (padded black — one solid color → one stable palette cell, flicker-free on a still), `"stretch"` skips aspect handling so the mode's resize distorts to fill. Exposed on slideshow only: a still image's pad bars quantize to a fixed bg0, whereas a video's per-frame bg0 churn would shimmer the bars.

## Composable scenes — `scenes.SourceScene` + `frame_source.py` + `generators.py` + `effects.py` + `audio_source.py` + `modulation.py` + `music_features.py`

A `SourceScene` (in `scenes.py`, `type = "generative"`) is the composable building-block scene: a **FrameSource** × an **AudioSource** × a display mode × an optional pixel **effect**, each chosen independently. The display mode is orthogonal to the source, so the same generator renders mhires/petscii/mcm purely by `display`.

#### `frame_source.py`

The `FrameSource` protocol (`read(t, modulation=None)`) plus `BaseFrameSource`.

#### `generators.py` — the `GenerativeSource` registry

Registered: `plasma`, `tunnel`, `fire`, `mandelbrot`, `moire2`, `halo`, `epicycle`, `hopalong`, `rorschach`, `hiphotic`, `metaballs`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `game_of_life`, `soap`, `fireworks`. All pure-numpy.

**The determinism contract.** On the unmodulated path, generators are **deterministic in `t`**: `render(t, None)` is byte-identical forever. The offline renderer and the drift tests depend on this. Even `fire` honors it, by scrolling a precomputed tileable turbulence texture as a pure function of `t` rather than running a stateful cellular sim.

The contract is upheld three different ways, depending on the generator:

* Randomness resolved **once at construction**, from a fixed seed (`rorschach`).
* Recomputed **from scratch every frame** (`hopalong`, `mandelbrot`).
* Replayed from a fixed seed up to the generation `t` implies (`game_of_life`).

Only `soap` and `fireworks` carry real incremental state — see Tier 3 below.

To add a generator: a `@register` subclass plus its name in `config._GENERATIVE_SOURCE_CHOICES` (drift-tested).

##### `mandelbrot` — the one recomputed field

The only generator whose per-pixel field is recomputed every frame instead of precomputed once. `t` drives an exponential zoom, `scale(t) = exp(zoom_speed·(t mod period))`, into a fixed "seahorse valley" point.

float64 precision bounds how deep any zoom can go before per-pixel spacing collapses into noise. So `period` = `ln(_ZOOM_LIMIT)/zoom_speed`, with `_ZOOM_LIMIT` ≈1e13 — safely inside float64's precision floor for the view's pixel spacing — is chosen so the zoom **wraps back to the starting view** at the boundary rather than degrading. That wrap is what keeps `render(t, None)` well-defined forever.

Escape-time iteration count is fixed regardless of zoom depth, deliberately not grown toward the precision limit: the output quantizes to a 16-colour C64 grid, so filament-level deep-zoom detail would be invisible anyway, and fixing it bounds per-frame cost at any depth. Interior (non-escaping) points are forced to black per-pixel, overriding the reactive brightness there.

##### The xscreensaver batch

Five generators from an xscreensaver-sourced backlog, ported as closed-form pure-numpy equivalents rather than literal X11 pixmap ports. Each maps a distinct facet of `MusicModulation` onto a distinct visual mechanism.

* **`moire2`** — sums two concentric-ring distance fields (`sin(dist * freq)`) whose centers drift apart via `sin(t)`. Reactive `beat_phase` breathes the separation, and each ring tracks a different `voice_freqs` entry. This is the classic beat-pattern interference that xscreensaver's `moire2.c` gets by XOR-compositing two arc bitmaps.
* **`halo`** — additively blends several soft gaussian circles drifting on independent orbits, evenly spaced at `t=0` with individually-tuned orbit rates drifting them in and out of alignment over time. Because it must stay pure in `t`, it cannot accumulate an un-cleared canvas the way `halo.c` does, so each halo's trail is faked by drawing it at a few trailing time-lags with decreasing brightness every frame. `level` grows every halo's radius; `onset` flashes in an extra center halo that is invisible at rest.
* **`epicycle`** — sums rotating phasors `r_i * exp(j*(w_i*t+phi_i))` and draws the arm chain plus a fading tip-trail (again via trailing time-lag echoes, not accumulation). Radii taper *geometrically* rather than by the stricter harmonic `1/(2i+1)` Fourier series, which collapses every arm past the first into an illegible cluster at only 5 terms. Reactive `voice_freqs` retune the first few arms' spin rates so the chain visibly tracks the tune; `level` scales every radius.
* **`hopalong`** — iterates Barry Martin's chaotic point-map (`x'=y-sign(x)*sqrt(|b*x-c|)`, `y'=a-x`) for thousands of starting points in parallel, numpy-vectorized across the batch (the map itself is sequential in the iteration count), into a log-scaled density accumulator. It re-runs the whole batch from scratch every frame — cheap, a few hundred vector ops — so a continuous drift of the `a`/`b` constants reshapes the attractor immediately. Chaotic sensitivity means even a small constant change visibly reshapes the whole point cloud. Driven by a slow sinusoid at rest, `level`/`beat_phase` continuously, and `onset` as a one-frame kick.
* **`rorschach`** — precomputes a single fixed-seed 2D random walk once at startup and reveals a growing prefix of it each frame, driven by a triangle wave in `t` (grow, then recede) so the loop never pops. Mirrored across the vertical axis for the ink-blot symmetry. `level` scales the revealed points larger; `onset` jumps the reveal fraction forward — a "restart" flash without discarding the walk.

##### WLED ports, Tier 1

The first batch of a WLED-effect-port initiative, from a survey of WLED's `wled00/FX.cpp` 2D matrix effects catalogued for porting.

* **`hiphotic`** — reimplements WLED's nested 8-bit trig interference, `sin8(cos8(x·speed/16+a/3)+sin8(y·speed/16+a/4)+a)`, in continuous float. Unlike plasma, the `t`-driven phase sits *inside* the inner cos/sin terms rather than being added at the end, so the combined field cannot be precomputed once and modulo'd per frame — only the raw pixel grids are cached, and everything else is recomputed every `render()` call. WLED's independent X-scale and Y-scale sliders collapse into one `scale` LIVE_PARAM, a deliberate simplification.
* **`metaballs`** — blends 3 moving ball centers into a classic inverse-distance field. All 3 paths are closed-form functions of `t` in WLED's own source too, since `beatsin8` is phase-linear in wall-clock time rather than a running accumulator. Ball 1 ports directly as a Lissajous sine pair. Balls 2 and 3 use WLED's `perlin8` point samples, for which this codebase has no primitive, so they are replaced with a 2-term incommensurate-frequency sine wander — the same pure-trig organic-motion trick `hopalong` and `epicycle` already use. A documented simplification, not a literal noise port.
* **`rotozoomer`** — samples a static precomputed XOR bit-pattern texture (`(x·4) ^ (y·4)`) through a rotating/zooming affine transform, via `cv2.getRotationMatrix2D` + `cv2.warpAffine` (the first use of `warpAffine` in this codebase; `BORDER_WRAP` mirrors WLED's modulo-wrapped texture lookup). WLED integrates its rotation angle once per render call (`angle -= 0.03 + (speed-128)*0.0002`), tied to WLED's own frame cadence rather than wall-clock time — incompatible with the pure-function-of-`t` contract. So the angle is redefined in closed form as `angle(t) = -speed·t`, the same "phase advances linearly with `t`" pattern plasma and tunnel already use for hue rotation. WLED's alternate Perlin-noise texture mode ("Alt") is not ported — a documented scope-narrowing and a candidate follow-up.

##### WLED ports, Tier 2 — the dot/line + trails family

WLED itself draws all of these by redrawing points and lines fresh every render call rather than accumulating them; its softening comes from `SEGMENT.blur`, not carried state. So like Tier 1, each ports as a pure function of `t` with no new determinism trick.

* **`lissajous`** — samples a fixed 256-point XY curve (`x=sin(theta·scale+phase)`, `y=cos(theta·2+phase)`) fresh every frame. Already the full closed curve at any instant, so unlike `halo`/`epicycle` it needs no synthetic time-lag echo.
* **`dna`** — samples two `pi`-phase-shifted sine strands across every column of the frame width, using WLED's own two-strand offset (`i·4` vs `i·4+128`). Fully sampled per frame, reading as a continuous double-helix trace without echoes.
* **`drift`** — samples a spiral arm (radii stepping outward from center, angle `t·(maxDim-i)`) fully every frame the same way. Always draws WLED's optional "Twin" mirror point, which sits behind a checkbox this codebase has no per-scene boolean for — an always-on simplification for a fuller default rose.
* **`colored_bursts`** — draws several lines from one common, slowly-orbiting point out to per-line endpoints on their own faster orbits. WLED's shared start point has no per-line phase offset; it is the *other* endpoint's `i·24`/`i·48+64` phase spread that fans the lines into a burst.
* **`dotswarm`** — collapses WLED's Black Hole, Frizzles, Sindots, Squared Swirl, and Drift Rose into **one** generator rather than four or five near-identical ports. All of them reduce to the same primitive: a handful of points independently orbiting via a bounded sine (`beatsin8`) at its own frequency. So this ports that shared primitive once, with a fixed varied per-dot frequency assortment (echoing the spread each WLED kin effect hand-picks) plus a fixed white center dot, Black Hole's signature.

`colored_bursts` and `dotswarm` are the two members of this batch that WLED only makes read as continuous through per-frame `fadeToBlackBy` accumulation — a handful of lines or dots would otherwise flicker as isolated marks. Both fake that persistence the same way `halo`/`epicycle` do: a short stack of trailing time-lag echoes at decreasing brightness, drawn oldest-first so the brightest, most recent position always paints on top.

All five reuse the `speed`/`scale` LIVE_PARAM names Tier 1 established, so they land on the WLED bridge's sx/ix sliders and MIDI control with no changes to either.

##### WLED ports, Tier 3 — the stateful sims

The batch the port catalogue flagged as needing an architecture decision, since generation N of a real simulation cannot be computed without generation N−1 — unlike every generator above.

**The decision: no new base class.**

* **`game_of_life`** stays a **pure** function of `t` anyway, using the trick `mandelbrot` and `hopalong` already established: replay the whole simulation from a fixed-seed initial soup for `floor(t / STEP_S)` generations on every call, capped at `_EPOCH_GENERATIONS` before reseeding. The cap bounds replay cost and stands in for WLED's adaptive stagnation detection — a fixed-length cycle instead of adaptive detection, the same kind of documented simplification as `rotozoomer`'s closed-form angle. It is Conway's Game of Life — WLED's `mode_2Dgameoflife` — B3/S23 via `np.roll` neighbor sums on a coarse, chunky-upscaled grid, with WLED's signature parent-color inheritance: a newly-born cell's hue is the mean of its live parents'.

  An instance-level cache keyed on the reachable `(epoch, generation)` pair — **not** on call order — makes sequential real playback cheap by stepping forward from the last-computed generation, without weakening purity. A cache miss, whether a new epoch or a `t` landing before the cached generation, always re-derives from the fixed seed. So the result never depends on *how* a given `t` was reached.

* **`soap`** (WLED's `mode_2Dsoap`) is a persistent color buffer smeared each tick by a slowly-rotating noise-driven flow field, reusing the `_periodic_value_noise` helper already built for `fire` and sampled twice for independent x/y flow components, rather than adding a Perlin primitive.

* **`fireworks`** is WLED's shared particle-system engine's flagship preset: a fixed-size preallocated numpy particle pool with vectorized position/velocity/age/life updates, and shells that launch, arc, and explode into fading bursts under gravity and drag. The same engine also drives WLED's Volcano, Ballpit, Waterfall, Impact, Attractor, and Galaxy presets, deferred as follow-up variants.

`soap` and `fireworks` are too expensive to replay from scratch every frame — a full-buffer `cv2.remap`, or particle position that depends on the whole integration history. So they carry **real incremental state** directly on the `GenerativeSource` instance (`self._last_t` plus a fixed-tick accumulator, the standard fixed-timestep-with-accumulator pattern, which handles variable frame arrival and dropped frames gracefully) rather than getting their own base class — the same way `effects.py`'s `TrailsEffect` already carries `self._prev` without one.

A call whose `t` doesn't advance, whether repeated or a backward jump, takes no step and re-returns the current cached frame. That keeps `render(t, None)` stable for a fixed, non-advancing `t`, which is what the shared determinism test actually checks. Note the honest limitation: unlike the pure generators, jumping directly to an arbitrary `t` on a fresh instance does **not** reproduce the same frame as advancing there gradually.

`GenerativeSource` gained a no-op `reset()` hook, mirroring `FrameEffect.reset()`, for `soap`/`fireworks` to override. It is not currently called by `scenes.py` — a fresh generator instance is built per scene entry, so state already resets naturally — and exists for parity and defensiveness.

#### `effects.py` — the `FrameEffect` registry

`trails`, `pulse`, `rgb_shift`, `blur`, plus the Live DJ/VJ Phase-3 VJ set `strobe`, `invert`, `mirror`, `posterize`. Applied in `scenes._render_with_overlays` before quantization, so every frame-bearing scene supports them.

`apply(frame, t, modulation=None)` takes the same `MusicModulation` snapshot the generators read, threaded from `SourceScene.process_frame` → `_render_with_overlays`; other scenes pass `None`.

* **`trails`** — lengthens its tail on a transient or loudness, so the comet blooms on the beat.
* **`pulse`** — beat-punches a center zoom on `onset`.
* **`rgb_shift`** — slews the red and blue channels apart on `onset`.
* **`blur`** — `cv2.GaussianBlur`, an enabler for future WLED dot/trail-family ports that lean on `SEGMENT.blur`.
* **`strobe`** — blanks the frame for the dark fraction of each beat (`duty` lit fraction, `rate` strobes/beat), phase-driven off `modulation.beat_phase`. The effect that most wants `mod_source = "clock"`: pointed at the beat grid it locks to the bar, pointed at SID audio it flashes on the beat envelope. Identity without modulation.
* **`invert`** — photo-negative crossfade (`mix` 0→1, `255 - px`). Static/live, not reactive.
* **`mirror`** — symmetry fold (`axis` = `horizontal`/`vertical`/`quad`). The one effect with a `LIVE_CHOICES` discrete knob rather than a scalar; it carries `set_live_choice`/`get_live_choice` so `midi_control._apply_param`'s choice branch (and the WLED bridge) cycle it with no effect-specific code, exactly like a display mode's choices.
* **`posterize`** — level crush to `levels` bands per channel (also pre-simplifies the frame for the palette reduction downstream). Static/live.

**The `modulation is None` path is byte-stable**: `trails` uses its configured decay; `pulse`/`rgb_shift`/`strobe` are the identity transform; `invert`/`mirror`/`posterize` are non-reactive and deterministic regardless. Non-reactive scenes, the offline renderer, and the determinism tests are therefore unchanged. `pulse`/`rgb_shift`/`strobe` only visibly react when handed a live feature stream — a `generative` scene with `audio_source = "sid"` **or** `"mic"` (`mod_source = "audio"`), or *any* scene under `mod_source = "clock"` once a `[performance]` beat grid is running.

`blur` differs structurally from `pulse`/`rgb_shift`: its identity guarantee comes from a default `intensity=0.0`, not from `modulation is None`. A nonzero `intensity` blurs every frame whether or not the scene is reactive, with a reactive onset adding a kick on top of the configured base — the same "base + kick" shape `trails` uses. `intensity` is used directly as `sigmaX`, and is named to match the existing `effect.intensity` live-param convention rather than a blur-specific name like `radius`, so it reaches the WLED bridge's ix slider and MIDI control with no changes to either.

##### The layerable chain (Live DJ/VJ Phase 3)

A scene holds an **ordered chain** `scene.effects: list[FrameEffect]`, not a single effect. `_render_with_overlays` runs the chain in order via `_apply_effect_chain`, at the same seam the single effect used to occupy. The legacy `scene.effect` is now a **back-compat property** over `effects[0]` (get) / a one-element chain (set), so the WLED bridge and any single-effect caller are unchanged. Config authors pick **either** the single `effect` **or** the `effects` list (mutually exclusive, validated in `validate_scene_cfg`); both bottom out in the same chain in `build_scene`.

Two per-layer knobs live on the `FrameEffect` base so every effect gets them:

* **`enabled`** — a bypass toggle. When `False` the render loop skips the layer entirely (exact identity), so a `fx_toggle` MIDI action drops a layer out and back in live. A plain bool (GIL-atomic on the reader thread); the skip is in the loop, not inside `apply()`, so a bypassed layer is byte-for-byte identity and the determinism guard holds with **any** subset disabled.
* **`mod_source`** — which `MusicModulation` feeder drives this reactive layer: `"audio"` (the scene's own feature stream — SID host-emulator or live-input analyzer — as `modulation`), `"clock"` (the Phase-1 `TempoClock` via `scene.clock_modulation`, stamped on by `Playlist._safe_setup`), or `"off"` (always the `None` baseline). `_effect_modulation` resolves it per layer; a non-reactive effect ignores the arg regardless. Set once per scene by `build_scene` (from `[[scenes]].mod_source`) onto every layer — the "synced to audio OR quantized via MIDI tempo" requirement, with no new effect code.

A layer that raises in `apply()` is **dropped from the chain** (not the whole scene torn down, and not the entire effect surface nulled as the pre-chain code did) so one bad effect can't kill a live set; the loop iterates a copy so the removal is safe mid-iteration.

To add an effect: a `@register` subclass plus its name in `config._EFFECT_CHOICES` (drift-tested, order-pinned to the registry). MIDI addresses a specific layer with the `fx<N>.<param>` / `effect[<N>].<param>` target grammar and toggles it with `fx_toggle` (`slot = N`, 0-based) — see the [`midi_control.py` Phase-3 note](control.md#fx_toggle--the-fxn-target-grammar-live-djvj-phase-3).
* `audio_source.py` — `AudioSource` protocol (`setup`/`teardown`/`position_seconds`/`features`). `NullAudioSource` (silence), `MicAudioSource` (live audio input — reactive by default, see [`audio_features.py`](audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input)), `SidFileAudioSource` (plays a `.sid` on the real chip — the audio half of WaveformScene, factored out so it pairs with any FrameSource). A SID-audio SourceScene is forced to host-DMA (`force_host_dma`) and fixed at VIC bank 0, so the payload must clear the display ($0400 for char, +$2000 for bitmap) — **char displays (mcm/petscii) are the most robust pairing**, but a bitmap display (mhires/hires) works with a tune that loads high enough to clear $2000. `run_sid_player` kicks its player via the firmware's `run_prg`, which re-inits the machine back to **text mode** — and `Scene.setup` configures the display's VIC registers *before* `audio_source.setup()` runs the player. So `SidFileAudioSource.resets_display = True`, and `SourceScene.setup` re-asserts the display mode *after* the audio source starts (mirroring WaveformScene's "VIC setup after the player" order); without this a bitmap mode renders its `$0400` colour nibbles as PETSCII. SID-structural helpers live in `sid_host_emu.py` (re-exported by `waveform.py`). `SidFileAudioSource.setup` also runs SID Player Autoconfig (`sid_autoconfig.apply_sid_autoconfig`) before `run_sid_player`, matching the tune's requested chip model to the U64's actual SID hardware — see the "SID Player Autoconfig" bullet in the `waveform.py` section below for the mechanism.
* **Music-reactive visuals** (`modulation.py` + `music_features.py` + `audio_features.py`): when a SourceScene plays SID audio and `reactive = true` (default), `SidFileAudioSource` spins up a `music_features.SidFeatureStream` — a persistent `SidHostEmu` + `SIDEmulator` + `PollThread` that runs the same tune in parallel and distills per-voice envelope/freq/gate into a small frozen `modulation.MusicModulation` (level / onset / beat_phase / bpm / per-voice freq+gate). It's **entirely host-side** (no extra U64 traffic) and mirrors WaveformScene's poll thread (wall-clock catch-up, multispeed rate detection). `SourceScene.process_frame` reads `audio_source.features()` and threads it into `source.read(t, modulation)`; the generator scales its own params (plasma: `beat_phase` cycles the hue with the tempo, `onset` kicks the hue + flashes brightness — degrades to baseline when silent). `bpm` is an onset-rate proxy (EMA of inter-onset intervals), not a true beat tracker; `beat_phase` is its integral, so estimate jitter never causes a phase jump. The visual math stays pure (modulation injected, `None` = pure-`t`); *how* features are measured is decoupled from *what reacts* — which is exactly what let a **second producer** drop in behind the same struct: `MicAudioSource` with `reactive = true` runs [`audio_features.AudioFeatureStream`](audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input) over a pre-DSP tap of the live input, so an instrument or mixer feed drives the same generators, effects and WLED broadcast with no changes to any of them. That producer also fills `MusicModulation.bands` (empty on the SID path, which reads envelopes rather than a spectrum), so bass punches brightness and treble shifts hue on the audio path only. Only `audio_source = "none"` has no feature stream (`features()` → None). `scripts/diags/render_offline.py` takes `--onset/--beat-phase/--level` to eyeball the reactive path offline.

## `overlays/`

Stackable scene decorations. Each overlay subclasses `Overlay` and registers via `@register("name")`. `config.scenes_from_config` builds them from `[[scenes.overlays]]` TOML blocks and `validate_for_scene` rejects incompatible combinations (e.g. a text overlay on an `mcm` scene — color RAM bit 3 reinterprets the cell as multicolor + halves horizontal resolution, so neither char glyphs nor folded bitmap glyphs render right there).

**Text overlays render on bitmap modes too.** A text overlay paints through `buffers["text"]` — a `text_surface.TextSurface` the scene's `compose()` stashes — instead of poking screen/color RAM directly. The surface reports its own `cols`/`rows` and folds each run into either char screen codes (`CharTextSurface`) or bitmap glyphs (`HiresTextSurface`: 40×25, glyph + FG/BG nibble per cell; `MHiresTextSurface`: 20 double-wide cols, c1=bg / c2=fg opaque box per cell, optional `text_double_height` 16px/12-row grid). Because the glyphs fold into the in-memory buffers *before* `push()`, the text rides the same host-DMA or REU bank-swap path as the frame — unlike the `menu` overlay's post-render direct writes, which skip REU-staged bitmap scenes. The shared glyph rasterizer is `bitmap_text.py`.

Restrictions:

* `REQUIRES_PETSCII = True` — the overlay paints text (screen codes + color). Accepted on any `is_petscii_compatible = True` char mode (`PETSCIIDisplayMode`, `BlankDisplayMode`). Rejects `mcm` (color RAM bit 3 reinterprets the cell as multicolor + halved horizontal resolution).
* `SUPPORTS_BITMAP_TEXT = True` (alongside `REQUIRES_PETSCII`) — the overlay folds its glyphs via the TextSurface, so it ALSO renders on bitmap modes (`is_bitmap_text_compatible = True`: `HiresDisplayMode`, `MultiHiresDisplayMode`). Set on the shared text bases (`corner_text`, `marquee`, `scrolling_text`) + `logo`, so every text overlay works on petscii/blank/hires/mhires. Overlays whose paint isn't a simple text run (the `spectrum_petscii` bar renderer) leave it `False` and stay char-only.
* `COMPATIBLE_MODES = ("a", "b", ...)` — whitelist of display-mode names this overlay supports. Empty tuple (default) = no restriction. Used for overlays that don't map onto the binary "PETSCII vs bitmap" split — e.g. `big_text` paints into blank or MCM buffers but not into a PETSCII webcam scene (where it would stomp the live-frame PETSCII glyphs).
* `REQUIRES_AUDIO = True` — needs `[audio]` enabled. `build_overlay` raises with a clear message otherwise.

The built-in overlays:

("text modes" below = petscii / blank / hires / mhires — folded into the bitmap on the latter two.)

| Overlay            | Restriction               | What it writes                                                                 |
|--------------------|---------------------------|--------------------------------------------------------------------------------|
| `scrolling_text`   | text modes                | One row of screen + color RAM, configurable row/speed/messages.                |
| `marquee`          | text modes                | One row, single text string, ticker-style continuous loop with separator.      |
| `rss`              | text modes                | Marquee fed by a background RSS/Atom fetch (stdlib `ElementTree`).             |
| `spectrum_petscii` | petscii / blank, audio    | A strip of cells (bottom / center / split mode), 8 bands × 5 cols.             |
| `clock`            | text modes                | Time/date in a corner; only updates when the formatted string changes.         |
| `weather`          | text modes                | Temp + conditions in a corner; background thread polls every N minutes.        |
| `callsign`         | text modes                | Static text in a corner. Single paint, then change-detect zero traffic.        |
| `countdown`        | text modes                | Time-until-target in a corner; auto-format or `{d}{h}{m}{s}` template.         |
| `network`          | text modes                | IP / hostname / U64 ping latency in a corner; background socket poll.          |
| `logo`             | text modes                | Multi-line PETSCII art from a `.txt` file at `corner` or explicit `row`+`col` (wide art clips on mhires). |
| `big_text`         | blank / mcm only          | Demo-scene 8×-scaled scrolling text (each source PETSCII char → 8×8 cells).    |

Most corner-positioned overlays (`clock`, `weather`, `callsign`, `countdown`, `network`) share `overlays/corner_text.py` — subclass `CornerTextOverlay` and just implement `compute_strings(t) → Optional[list[str]]`. The base handles change-detection, blanking-on-shrink, and teardown cleanup.

`marquee` and `rss` share `overlays/marquee.py:MarqueeBase` — subclass and implement `_current_text()`.

Audio overlays read recent float samples from `AudioStreamer.get_recent_samples(n)`, which exposes a 2048-sample tap filled by every input path (mic, WAV, PyAV).

`Overlay.is_busy()` (default `False`) lets a slow-paint overlay defer the scene's auto-advance. When a scene's `duration_s` timer expires, the Playlist checks every attached overlay's `is_busy()` and, if any returns True, flips `is_done` back to False so the scene runs another frame. `big_text` uses this in `loop = false` mode to make the Playlist wait for the last message to finish scrolling off-screen before the interstitial appears. In the default `loop = true` mode, `is_busy()` always returns False (the message list is effectively infinite — busy-defer would freeze the playlist) and `duration_s` is the source of truth. **CTRL skip always wins**: when `skip_event` is set, `is_done = True` is forced regardless of busy state — the busy guard runs above the CTRL branch, so the CTRL branch overwrites it. For the busy-defer to actually paint frames past `duration_s`, the scene's `process_frame` must keep rendering after the deadline; `BlankScene` does (it returns `still_active = False` but renders the frame first), `WebcamScene` and `VideoScene` short-circuit.

**Single-scene mode**: when `len(scenes) == 1` at `Playlist.__init__` (or after a reload), `Playlist.single_scene` is True. `_advance` skips the interstitial path entirely — the one scene is set up directly on first call, and on `is_done` it's torn down and re-set-up back-to-back so it loops forever. CTRL skip events are dropped (with `log.debug`) and the event is cleared so it doesn't accumulate; C= pause/resume still work. `scenes_from_config` also short-circuits `interleave_videos` when the user-defined playlist is a single scene (an inserted video would promote the playlist to 2 scenes and silently defeat the mode). This is the mode every file in [config/examples/](../../config/examples/) runs in.

**Playlist loop control**: `[playlist] loop` (also `--loop` / `--no-loop`) controls what happens at the end of the playlist. Default `true` preserves the looping behavior above. `false` makes `_advance` set `stop_event` instead of looping — single-scene mode tears down after one play; multi-scene tears down after one full pass through the scene list. Used for "play one video and exit" and "play these N videos then quit" workflows. Live-streaming scenes (webcam, blank) typically leave it at the default and run until the user kills the streamer.

## `interstitial.py` + `backgrounds.py`

`InterstitialScene` is what plays between scenes ("UP NEXT: …"). It renders two centered text lines (the label `UP NEXT:`, a blank row, then the upcoming scene name) on top of an animated parallax background. Color is configurable (`rainbow` gives each line a different color from the rainbow palette).

`backgrounds.py` registers 7 styles: `starfield`, `petscii_bars`, `raster_bars`, `checker`, `nature`, `city`, `none`. Each implements `render(t, top_rows, bottom_rows, bg_color) -> (chars[1000], colors[1000])` that fills only the strips above and below the text — the InterstitialScene writes its text into the middle rows on top. `"random"` rotates through styles per setup() call. All writes go via `write_region` so the delta cache absorbs the static cells.
