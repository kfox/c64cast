# SID playback & the oscilloscope

Playing real SID music and drawing it: the 6502 player PRG, the host-side emulator that recovers register state, and the three scenes built on the shared oscilloscope renderer.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`voice_scope.py` — shared 3-voice oscilloscope renderer](#voice_scopepy--shared-3-voice-oscilloscope-renderer)
* [SID player PRG — 6502 player, relocation, and per-call banking](#sid-player-prg--6502-player-relocation-and-per-call-banking)
* [`waveform.py` + `sidemu.py` + `sid_host_emu.py` — SID oscilloscope scene](#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene)
* [`midi_scene.py` — MidiScene (live MIDI → SID + oscilloscope)](#midi_scenepy--midiscene-live-midi--sid--oscilloscope)
* [`asid.py` + `asid_scene.py` — AsidScene (ASID client → real SID + oscilloscope)](#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope)

---

## `voice_scope.py` — shared 3-voice oscilloscope renderer

`VoiceScopeRenderer` is the SID-source-agnostic hires oscilloscope renderer, factored out of `WaveformScene` so `MidiScene` can reuse it. It's a **mixin** (both `WaveformScene` and `MidiScene` are `class X(VoiceScopeRenderer, Scene)`), so the renderer reaches its host's state as `self._<attr>` and `WaveformScene`'s byte output is the mixin's byte output — its test suite is the regression guard for both scenes. It owns the layout constants (`BITMAP_STRIPS`, `TITLE_ROW`/`META_ROW`, the `D011`/`D016`/`D018` poke values), the VIC hires bring-up (`_apply_vic_hires_bank`, which calls the shared `modes.engage_bitmap_mode` primitive — the SAME clear-then-flip path the Hires/MultiHires display modes use, see the modes.py "Bitmap engage clean-field" note; the scope passes its relocated bank + `write_region` clear IDs as arguments), the glyph + text-row rasterizer (`_paint_text_row`, `_load_glyphs`, `_ascii_to_screen_code`, `_layout_lr`/`_layout_lcr`), the per-voice color helpers, the three render paths (`_render_voice_fast`/`_scroll`/`_echo` → `_render_hires`), and the knob parsing + buffer allocation (`_init_scope_knobs` / `_alloc_scope_buffers`). A host scene satisfies a documented attribute contract (`api`, `emulator`, `_reg_lock`, `_screen_base`/`_bitmap_base`/`_dd00`/`_d018`, …) and supplies its own text-row CONTENT. `waveform.py` re-exports the consts that live here, so `from .waveform import …` (config, tests) resolves them too.

## SID player PRG — 6502 player, relocation, and per-call banking

SID playback uses a hand-encoded 6502 player (73 bytes) plus a SHIFT-driven re-INIT stub (35 bytes), both relocated per tune by `_choose_player_layout` in [api.py](../../c64cast/api.py).

### Layout and relocation

Default layout is the player at `$C300` and the stub at `$C400`, used whenever there is no conflict. A SID whose payload would overlap gets a contiguous bundle (`player_base + 80 = stub_base`) placed in the largest footprint-clean RAM hole, falling back to just past or just below the payload.

`_build_basic_sys_stub` builds the BASIC SYS stub's decimal argument dynamically to match the chosen `player_base`.

`$C300` was chosen because [audio.py](../../c64cast/audio.py) owns `$C000-$C2FF` for the NMI DAC and REU pump. The relocation picker refuses any layout that would overlap that region.

### Per-call `$01` banking

The player banks `$01` **per call**. `_init_bank_for` / `_play_bank_for` patch the value to `$36` — BASIC ROM banked out, KERNAL and I/O kept — around a `JSR init` or `JSR play` whose entry lives under BASIC ROM (`$A000-$BFFF`, e.g. Hyperion 2 at `$AE2A`). Without that, the call executes ROM instead of the tune's RAM, landing on the ROM SYNTAX-error stub and printing `?SYNTAX ERROR IN 10`.

It then restores `$37`, the resting default. Tunes like Comic Bakery that read BASIC ROM *as data* need it mapped between calls.

Restoring per call, rather than setting `$36` once and leaving it, is what keeps tunes like Election alive: they assume the `$37` resting environment between PLAY calls and crash without it.

### Runtime behavior

The real 6510 calls INIT once, and PLAY at IRQ time. The IRQ handler chains to kernal `$EA31`, so keyboard scan (`$028D`) and cursor-blink suppression both survive.

After installing the IRQ vector the player **spins forever** (`JMP *`) rather than RTSing back to BASIC. INIT routinely clobbers BASIC zero-page state, so a return would print a syntax error on the next interpreter step.

### What is refused

PSID only. Rejected: RSIDs, SIDs whose `load_addr` < `$0820`, SIDs whose `play_addr` is zero, and SIDs under KERNAL ROM (`$E000-$FFFF`).

The PAL/NTSC speed flag is ignored — a v1 limitation; the kernal-default CIA #1 Timer A rate is used.

### Backend-agnostic orchestration

Parse, layout, build, divider-tune, and subtune-reinit all live in `_SidPlayerBackend`. Only the *kick* differs, via the abstract `_launch_sid_player`.

**Ultimate** — POSTs the `SYS` stub to `run_prg`.

**TeensyROM** — uses a pure-DMA `$0314` **vector swap** instead, the same primitive as `cue_song_reinit`. Over the running IRQ-enabled clear-loop it DMAs the blobs, then swaps `$0314` to the re-INIT stub, which the next kernal IRQ runs to `JSR init` and install the PLAY handler. No LaunchFile, reset, or boot — therefore no async-boot race.

`run_sid_player(defer_audio=True)` plus `begin_sid_audio()` split load from start. That lets WaveformScene paint the scope **before** the first note on TR, or assert the bitmap after the player as before on U64, where `run_prg` resets the VIC. The scene anchors its host-emu clock to `sid_audio_start_time()`.

The TR path is gated on `supports_read`, meaning cycle-clean firmware v0.7.2.5+; the spin-stub idle on older firmware masks IRQs so the swap cannot fire, and older firmware raises `BackendCapabilityError`.

The TR also has no REUWRITE, so `cli._coerce_reu_for_backend` forces `use_reu_pump` and any explicit `use_reu_staged=true` off on a no-REU backend, leaving host-DMA NMI DAC audio and host-DMA video. `--doctor` reports this.

See [caveats.md](../caveats.md) → "SID playback uses a C64-side player PRG", including the TeensyROM vector-swap subsection, for the full rationale.

## `waveform.py` + `sidemu.py` + `sid_host_emu.py` — SID oscilloscope scene

`WaveformScene` (inherits `VoiceScopeRenderer`) plays a SID file on the U64 via `api.run_sid_player(...)` (DMA SID payload + 73-byte 6502 player relocated per-tune by `_choose_player_layout` — default $C300, bumped past the SID payload on overlap — then POST a matching `10 SYS <player_base>` BASIC stub via `runners:run_prg`) and visualizes the three SID voices' waveforms across the full screen. Display is bitmap-only (320×200 hires). Voices stack vertically — voice 1 top, voice 3 bottom.

The bottom two text rows carry the tune metadata: the **title row** (`_build_title_line`) is the song title left-justified + composer right-justified, fixed across subtunes; the **metadata row** (`_build_metadata_line`) is `SONG xx/yy` (zero-padded to `num_songs`' width) + copyright left-justified, and the SID's composed-for clock + chip right-justified (e.g. `PAL 8580`). When the SID targets one definite standard that differs from the current playback system (`[ultimate64].system`), the clock reads `native→system` (e.g. `PAL→NTSC 8580`) so the mismatch is visible; the `→` is a right-arrow synthesized by horizontally mirroring the ROM left-arrow glyph (`voice_scope._mirror_glyph_h`, blitted via `_paint_text_row`'s `glyph_overrides` — the C64 charset has no native `→` cell). Ambiguous (`PAL+NTSC`), unknown, or matching standards show the native value alone. Only the metadata row's song number changes on a SHIFT subtune switch, so `cycle_style` repaints just that row (the title row is untouched).

The firmware's `runners:sidplay` endpoint is deliberately avoided: on firmware 3.14d it draws its own "ULTIMATE C-64 SID PLAYER" UI on the HDMI scaler that covers everything we paint into VIC RAM. PSID-only — RSIDs (which install their own raster IRQ in INIT), tunes whose `load_addr` is below `$0820` (would collide with the BASIC SYS stub), tunes whose `play_addr` is zero (INIT installs own IRQ), and tunes with code/data under KERNAL ROM (`$E000-$FFFF`, where the player can't bank KERNAL out without losing its `$EA31` IRQ chain) are refused at scene setup with a clear error. Tunes under **BASIC** ROM (`$A000-$BFFF`) are supported — the player banks BASIC out per-call around the affected `JSR init`/`JSR play` (`$01 = $36`; see below). PAL/NTSC speed flag is ignored — the kernal's default CIA #1 Timer A rate is used. See [docs/caveats.md](../caveats.md) for the full design discussion.

The visualization is driven by a hybrid setup:

* **Audio** comes from the U64's native SID chip (the audience hears the real hardware).
* **Per-voice waveforms** come from a Python-side `SIDEmulator` in `sidemu.py`. The U64's FPGA SID is faithful to real hardware — `$D400-$D418` is write-only and reads return open-bus zeros, so we can't ask the U64 what the SID is doing. Instead, `SidHostEmu` in `sid_host_emu.py` runs the same SID file in parallel on a host-side [py65](https://github.com/mnaberez/py65) pure-Python 6502. A `TrappedRam` wrapper around the emulator's 64 KB array intercepts writes to `$D400-$D418` into a 25-byte shadow. The background poll thread ticks the host emulator at the system video rate (60 NTSC / 50 PAL — the SID's effective PLAY-per-frame cadence on a kernal IRQ) and feeds the shadow snapshot to `SIDEmulator`, which mirrors per-voice waveform-select / pulse-width / ADSR state and synthesizes samples on demand. Phase is owned by Python (not synced to the real chip), so what you see is a faithful per-voice oscilloscope trace at the right frequency and envelope — not a phase-accurate scope of the audio output. PSID validation is shared with `api.run_sid_player` via `parse_psid_for_player`, so a tune the U64 side refuses is also refused at host-emulator construction with the same error.
* **Multi-SID tunes (2SID/3SID)** are auto-detected and shown as a **split scope** — one side-by-side window per SID chip in each voice row (chip 0 left … chip N right), the same layout `AsidScene` uses. Detection is `sid_host_emu.detect_sid_addresses`: the PSID v3/v4 header's second/third-SID address bytes (`$7A`/`$7B`, each encoding a `$Dxx0` base as `$D000 | byte<<4`), raised by an HVSC-style `_<N>SID.sid` filename hint when the header understates the count (the hint appends canonical stride-`$20` bases since it carries no addresses). One `SidHostEmu` runs the tune; its `TrappedRam` shadows **all** the chips' register banks (one 25-byte shadow per base — the single 6502 already writes every chip), and `WaveformScene` feeds each bank into its own `SIDEmulator`/scope window (`regs(bank)` / `retriggers(bank)`). For **audio** on the U64, `_apply_sid_hw_config` maps the U64's extra SID cores to the tune's own addresses (via `asid_sidmap.plan_sid_map_for_addresses`, which realizes the file's fixed `$Dxxx` bases on ≤2 sockets + 2 UltiSID cores, falling back to the canonical `plan_sid_map` layout when the exact set isn't hardware-realizable) — snapshotted before, restored on teardown by the shared `sid_hw_config` helpers. Single-SID output is byte-identical to the pre-multi-SID renderer (one window). Backends without a SID config API (TeensyROM) still show every chip's scope; only `$D400` is audible there.
* **SID Player Autoconfig** — see the dedicated subsection below.
* **Combined waveforms** (multiple waveform-select bits set) are rendered by `sidemu.voice_samples` as a **12-bit bitwise-AND** of the selected waveforms' unsigned oscillator outputs — the SID wires those outputs onto a shared bus, so the AND approximates the real chip's sparse "metallic" combined shape (faithful in character, not chip-exact — an accurate model needs reSID-style per-chip sampled tables, a noted future refinement). Single-waveform output is byte-identical to the pre-combined code (only 2+-bit voices change). `primary_waveform` (priority noise > pulse > sawtooth > triangle) is still used for the `per_waveform` color pick + the silent check, not the trace shape. **Caveat (HW-confirmed):** the AND model *over*-represents sawtooth combos — on a real 6581 anything containing sawtooth ANDs down to near-silence (`pulse+triangle` is the one combination that reliably sounds), so MidiScene keeps saw/noise combos out of its interactive SHIFT/PC rotation (see `_WAVEFORM_CYCLE` in [c64cast/midi_scene.py](../../c64cast/midi_scene.py)); they're still settable via config. Closing that visual-vs-audio gap fully is what the chip-accurate tables would buy.
* **No filter, no master volume**: irrelevant to the per-voice oscilloscope view.

Coloring modes:

* `per_voice` — each voice gets a fixed C64 color from `voice_colors`.
* `per_waveform` — color reflects the currently-selected wave type (e.g. cyan for pulse, light red for sawtooth). Color RAM is rewritten only on transitions, not every frame.

Teardown order matters: `api.restore_kernal_irq_vector()` puts `$0314` back to `$EA31` first (so our IRQ handler is unhooked and PLAY stops being called), then `api.flush()`, then `api.silence_sid()` writes 0 to `$D418` and clears each voice's gate. If silence ran first, the IRQ could fire between the volume-clear and the gate-clears and PLAY would rewrite both. No reset, so the next scene paints over the waveform without the BASIC banner flashing.

The scene runs for a fixed `duration_s` — `runners:run_prg` doesn't surface a "finished" signal for the BASIC GOTO loop. Use SongLengths data via `[playlist].songlengths_file` if you have it; otherwise pick a duration that matches the tune.

Visualization knobs (all compose; defaults preserve the redraw-from-scratch, wallclock-locked behavior so existing configs render identically):

* `time_base = "wallclock" | "auto"` — `auto` derives the per-voice time window from `v.freq` so `auto_cycles` complete cycles always fit, regardless of pitch. Silent voices (freq=0, wave=off, or envelope=0) fall back to wallclock per-voice so the trace doesn't collapse to a flat line on a divide-by-zero.
* `persistence = "off" | "short" | "medium" | "long" | "random"` — replaces the per-frame-cleared bool canvas with a per-voice `uint8` intensity strip that decays each frame (faded pixels fall under a fixed mid-scale threshold and turn off). `random` resolves to one of the named presets at scene setup (same sentinel pattern as `petscii_styles`); the resolved name is logged on the scene's startup line.
* `scroll_columns = 0 | N | [N1, N2, N3]` — per-voice FIFO: shift the intensity strip left by N columns and draw only the new N columns on the right edge. Scalar broadcasts to all three voices; list assigns per voice (so one strip can scroll fast, one slow, one stay redraw-style). Scroll mode rewrites the whole strip per frame and busts the dirty cache on purpose; cost is bounded (≈700 KB/s of DMA at 60 fps, well under the ceiling). SHIFT-cycle zeroes the strip buffers so a `persistence = "long"` trail from the prior subtune doesn't ghost-merge into the new one.

### SID Player Autoconfig

`sid_autoconfig.py`, config `[ultimate64].sid_model`.

Matches each chip's **model** (6581/8580) to what the tune's PSID header requests. This is orthogonal to the address routing above, which only ensures a chip is *audible* somewhere — not that it is the *right-sounding* chip.

**Decoding the header.** `sid_host_emu.parse_sid_header` decodes `SidHeader.sid_models` — one entry per chip present, parallel to `sid_addresses` — from the PSID v2+ flags word at header offset `$76-$77`:

| Field | Bits | Byte |
| --- | --- | --- |
| sidModel1 | 4-5 | low (`$77`) |
| sidModel2 | 6-7 | low (`$77`) |
| sidModel3 | 8-9 | high (`$76`, bits 0-1) |

Each entry is gated on the same version and address-byte conditions that make its chip's `sid_addresses` entry exist, mirroring the firmware's `ConfigSIDs`.

**The decision function.** `sid_autoconfig.plan_sid_model_config` is pure. For each chip with a definite model requirement — a `None`, `"?"`, or `"6581+8580"` header value is always a no-op — it checks whatever currently answers that chip's address via `sid_hw_config.detect_socket_models`, reading `"SID Detected Socket N"`, then:

1. Already matches → no-op.
2. The *other* physical socket reports the required model → remap that socket's address, using the same `SID Addressing` / `SID Sockets Configuration` PUTs that `asid_sidmap.plan_sid_map_for_addresses` uses.
3. Otherwise → fall back to a free UltiSID core, setting its `"UltiSID N Filter Curve"` item (category `"UltiSID Configuration"`, confirmed live via `GET /v1/configs`) to a fixed representative curve, `"6581"` or `"8580 Lo"`. The full enum also offers `"8580 Hi"`, `"6581 Alt"`, `"U2 Low"`, `"U2 Mid"`, and `"U2 High"`, none exposed as a config knob in this pass.
4. Otherwise → warn and leave the chip unchanged.

**The setting.** `"auto"` (default) reads the header per chip. An explicit `"6581"`/`"8580"` forces that model for every chip, ignoring the header. `"off"` disables header inspection entirely.

**Multi-SID does not use this pass — it routes and matches in one.** `plan_sid_map_for_addresses` takes the header's `required_models` and the live `socket_models`, so a socket claims an address only when it carries the model that chip asked for; unclaimed sockets are disabled and each core's filter curve is set from the model its chips want. See [Multi-SID on the U64](#multi-sid-on-the-u64-asid_sidmappy).

**Why one pass, and not route-then-correct.** Splitting the work — route first, fix models after — cannot be made correct, because the two planners decide chip placement from different premises and the second silently invalidates the first. Reproduced on hardware with 6581s in both sockets, playing Jammer's *Light Years* (3 chips at `$D400/$D420/$D440`, all tagged 8580):

1. Routing is model-blind, so it seats chips 0-1 on the 6581 sockets and chip 2 on UltiSID 1 at `$D440`.
2. The model pass sees two 8580-wanting chips on 6581s, finds no matching socket, and moves them onto UltiSID 1 (`$D400`) and UltiSID 2 (`$D420`) — **taking the core chip 2 was given**. `$D440` is left addressed to nothing and chip 2 goes silent, while both sockets stay `Enabled` and answer `$D400`/`$D420` alongside the cores.
3. Panning then reads `SidMap.sources`, which describes the pre-correction placement, and pans sources the tune is no longer using.

**Where the pass still runs.** Single-SID tunes (nothing to route, but a tune requesting 8580 on a 6581-socketed `$D400` still needs remapping) and the canonical fallback layout `plan_sid_map`, which is chosen only when the tune's exact addresses aren't realizable and is model-blind. `WaveformScene._plan_multi_sid_map` reports which case applied; the fallback re-derives the model plan **after** applying the map, against the now-current addressing.

**The single-snapshot rule.** Address routing, model correction and the mixer changes all merge under **one** snapshot taken before any is applied. Two sequential `snapshot_sid_config` calls would capture the first change as if it were the original state, corrupting the teardown restore.

**The other call site.** `SidFileAudioSource` — the `generative` + `audio_source = "sid"` path in `audio_source.py` — has no address-routing counterpart, so it calls the simpler `sid_autoconfig.apply_sid_autoconfig` wrapper directly, with its own snapshot and apply, restored in `teardown()`.

Both call sites are best-effort, and no-op on a backend without a SID config API (TeensyROM).

> A genuinely fixed physical 6581/8580 chip cannot be reconfigured to the other model. Autoconfig can only *route around* it, never transmute it — see [caveats.md](../caveats.md).

`tests/test_sid_autoconfig.py` carries the decision-matrix coverage.

### SID panning

`sid_panning.py`, config `[ultimate64].sid_panning`. U64 only.

Spreads a tune's SID chips across the stereo field: a single SID centered, a multi-SID tune fanned out so each chip occupies its own place in the image. Applied live before playback, restored at teardown — the same best-effort snapshot/apply/restore contract as address routing and model autoconfig, and folded into the **same** saved-config dict so one restore puts everything back.

**Panning is per audio *source*, not per address.** The U64 mixes each source — physical SID socket 1/2, UltiSID core 1/2 — at its own pan, via the `Audio Mixer` category items `Pan Socket 1`/`Pan Socket 2`/`Pan UltiSID 1`/`Pan UltiSID 2` (enum `Left 5 … Left 1, Center, Right 1 … Right 5`, confirmed live against a U64). So a chip is panned wherever it was *routed*, which is why panning runs **last**, after any address remap has decided that placement.

Where the per-chip source comes from:

| Case | Source of truth |
| --- | --- |
| Remapped multi-SID (WaveformScene, AsidScene) | `SidMap.sources` — a tuple parallel to `SidMap.addresses`, populated by both planners in `asid_sidmap.py` |
| Single-SID / no remap (WaveformScene, `SidFileAudioSource`, AsidScene at setup) | `sources_for_addresses` → `sid_hw_config.current_source_map`, the live addressing read (shared with `sid_autoconfig`, which moved its `_current_addr_map` body there) |

Panning runs **after** address routing for the same reason, and in `AsidScene._reconfigure_chips` it runs after `_set_window_count` (which resets the column order).

**The list is indexed by source, capped at 4.** There is exactly one pan control per source, so `sid_panning` entry *k* positions the *k*-th source the tune claims (`distinct_sources`, first-claim order) — **not** chip *k*. Through 4 chips the two coincide, since each chip lands on its own source. A list longer than `MAX_PANNED_SOURCES` (4) could never take effect and is rejected at config load.

**The achievable count is lower when the tune doesn't use both sockets.** The ceiling is 2 (UltiSID cores) + 1 per socket *playing a chip* — which is not the same as populated, since model-aware routing skips a socket whose chip is the wrong model for the tune. With **no socket in use there are only two distinct pan positions**, so a 3+ chip tune necessarily doubles chips onto a shared pan; `_warn_if_sources_limited` warns at apply time (and separately when the config carries more entries than the machine can use).

**Values and defaults.** An entry is an int `-5..5` (negative = left, `0` = center) or a label string; both normalize through `pan_to_label`. A configured list wins, truncated or center-extended to the source count; empty means `default_pan_spread`:

| Sources | Spread |
| --- | --- |
| 1 | `[0]` (Center) |
| 2 | `[-3, 3]` |
| 3 | `[0, -3, 3]` |
| 4 | `[-2, 2, -5, 5]` |

These are ordered by **musical importance**, not as a uniform fan: with an odd count the primary chip sits dead center and the rest flank it; with an even count the first two chips sit closest to center and later ones spread wider.

**A spread the hardware can't carry collapses to center.** When chips outnumber sources — a 3-SID tune on a machine with no socketed 8580, where all three land on the 2 UltiSID cores — the table's spread would throw two chips hard left against one hard right, which misrepresents the tune rather than opening it up. `default_pan_spread(n_sources, n_chips)` returns all-Center in that case. This is a *default*, not a cap: an explicit `sid_panning` still spreads doubled-up sources however the user asks.

**The scope's columns follow the pans.** Because the spreads are importance-ordered rather than monotonic, raw chip order would draw the 3-SID default as `Center | Left 3 | Right 3` while it *sounds* left-to-right as `Left 3 | Center | Right 3`. So `apply_panning` also returns a `window_order` (`window_order_for_pans`: chips sorted by pan, ties keeping chip order), which the scene hands to `VoiceScopeRenderer.set_window_chip_order`. Scope column *w* then shows `window_order[w]`, and `_scope_emulators()` — the single choke point every window-indexed render/color path goes through — serves the emulators in that order. The 3-SID default therefore puts the primary chip in the **middle** column; the 4-SID default puts chips 1-2 in the two middle columns. An all-equal or single-chip set yields the identity order, so those renders are unchanged, and `_set_window_count` resets to identity (the panning pass re-applies afterward).

**Only differences are written.** `apply_panning` reads the mixer once and PUTs only the sources whose pan actually changes, returning just those originals. A tune whose panning already matches (the common single-SID-on-a-centered-mixer case) writes nothing at setup and restores nothing at teardown.

Applies to the three tune-playing scenes: `WaveformScene`, `AsidScene` (at setup for the initial chip, and again on every `_reconfigure_chips` remap so the spread tracks the live chip count), and `SidFileAudioSource` (the `generative` + `audio_source = "sid"` path). **`MidiScene` is deliberately excluded** — it is a live instrument rather than a tune, always one voice-set at `$D400`, and carries no SID-hw snapshot/restore scaffold; pan it from the U64's own mixer if wanted.

`tests/test_sid_panning.py` covers the pure planner + the diff-only apply.

### SID volume

`sid_volume.py`, config `[ultimate64].sid_volume`. U64 only.

The panning sibling, and the reason it exists: **the U64 mixes each source at an independent level, and the two UltiSID levels are commonly `OFF`.** Routing a chip onto an UltiSID core — which multi-SID routing and model autoconfig both do freely — then produces silence *with no error anywhere*: the chip is mapped, the player writes to it, and nothing comes out. Confirmed live on a stock machine (`Vol UltiSid 1`/`2` = `OFF`, `Vol Socket 1`/`2` = `" 0 dB"`), which silently drops the third chip of any 3-SID tune and every ASID chip past the second.

The mirror failure is just as real, and is why unused sources are muted rather than left alone: a tune that deliberately uses the socketed chips is doubled by an UltiSID core still mapped at the same address with its level up — including the LED mirrors this codebase now sets up deliberately.

**The policy**, one source at a time (`target_level`):

| Source | Level |
| --- | --- |
| In use, `sid_volume` entry set | that entry |
| In use, currently `OFF` | `" 0 dB"` |
| In use, at some other level | left alone — a rig trimmed to `-6 dB` is a deliberate choice |
| Not in use | `OFF` |
| Absent from the mixer read | left alone — writing it would make a change with no original to restore |

**Same shape as panning throughout.** A pure planner (`plan_sid_volume`, `resolve_volumes`, label/int conversion) plus one best-effort `apply_volume` that reads the mixer once, writes only what differs, and returns the originals for the caller's restore snapshot. Source derivation is shared, not duplicated — `sid_panning.distinct_sources` decides which sources a tune claims and in what order — so `sid_volume` is indexed by **source**, exactly like `sid_panning`: entry *k* is the *k*-th source claimed, capped at 4.

A configured list is truncated to the source count and padded with *auto*, not with a level: padding with a level would let a one-entry list silently dictate every other source.

**Values.** A dB int (`0`, `-6`, `3`), a label (`"0 dB"`, `"-6 dB"`, `"off"`), or a stringified int. Two firmware traps, both confirmed live via `GET /v1/configs/Audio%20Mixer`:

* the volume items spell it `Vol UltiSid 1`/`2` (lowercase `id`) while the pan items spell it `Pan UltiSID 1`/`2` (uppercase `SID`). The inconsistency is the firmware's.
* every non-negative level carries a **leading space** — `" 0 dB"`, not `"0 dB"`. An exact-match comparison against a stripped string never matches, so the mixer would be rewritten on every setup and the "no change ⇒ no writes" contract would be quietly lost.

The ladder is also **not** a uniform 1 dB fan — `OFF`, `-42`, `-36`, `-30`, `-27`, `-24`, then every dB from `-18` to `+6` — so an int with no representation (`-20`) is rejected at config load rather than snapped to a neighbor.

Applied at the same three call sites as panning, folding into the same restore snapshot: `WaveformScene._apply_sid_volume`, `AsidScene._apply_sid_mixer` (setup and every remap), and `SidFileAudioSource._apply_sid_mixer`.

`tests/test_sid_volume.py` covers the conversions, the policy, the pure planner and the diff-only apply.

### LED mirroring of socketed SIDs

The U64's built-in LED display is driven by **UltiSID core activity**, so a tune playing entirely on socketed chips lights nothing unless a core is also listening at those addresses. Both planners therefore point leftover cores at the socket-served addresses instead of unmapping them (`asid_sidmap.mirror_bases`) — which is how the firmware ships by default (`UltiSID 1 = $D400`, `UltiSID 2 = $D420`, both at `Vol OFF`).

The mirrors carry no chip of their own, so they never appear in `SidMap.sources`, which means `sid_volume` classifies them as not-in-use and mutes them. That is the whole safety story: LEDs from the core, audio from the real chip, and nothing doubled.

Mirroring is skipped unless the UltiSID split is off, since a split core answers a window of 2 or 4 addresses whose extra instances would collide with the real chips. That costs nothing in practice — a split is only ever chosen when both cores are already hosting real chips, leaving nothing spare to mirror with.

**Single-SID tunes are deliberately not mirrored.** They never reach a map planner (there is no address to route), so nothing touches the UltiSID addresses — and the firmware default already parks the cores at `$D400`/`$D420` with `Vol OFF`, so the LEDs light anyway. Adding a mirror there would mean the single-SID path starts writing addressing config it otherwise never touches, breaking the HW-verified rule that an already-matching single-SID tune produces **no REST traffic at all**. The cost is that a machine which has explicitly unmapped its cores gets no LEDs on single-SID tunes; set `UltiSID 1 Address` back to `$D400` on the U64 itself if that matters.

## `midi_scene.py` — MidiScene (live MIDI → SID + oscilloscope)

`MidiScene` (inherits `VoiceScopeRenderer`) turns the C64 into a 3-voice MIDI sound module and visualizes it with the **same** hires oscilloscope as `WaveformScene`. Note on/off → voice freq + gate; pitch-bend → ±2 semitones on gated voices. **Voice allocation** layers a mono melody over a polyphonic sustain pad: held notes keep their voice, and a new note over capacity steals the *most-recently-started* voice (`max` t_changed) so the older/held notes form a stable pad while an overlapping line/arp cycles on the top voice; freeing a voice resurrects the most-recent still-held suspended note (LIFO). **Gate-edge hard restart:** the real SID re-attacks only on a gate 0→1 edge, so re-using an already-gated voice (re-press / steal / trill) writes a gate-off control byte *before* the new voice block — without it the chip changes pitch but never re-triggers (silent note while the host-emulator waveform, fed a `retrigger` flag, still moves). The MIDI reader thread coalesces continuous-controller floods (wheel sweeps) to ≤60 Hz so they can't burst the DMA socket; notes stay immediate.

### Per-voice multi-waveform
Each voice has its own waveform: `self.voice_wave_bits[idx]` (authoritative, combos allowed) and `voice_wave_names[idx]`, built from `midi_voice_waveforms` — empty means all voices share `midi_waveform`. `_program_voice` ORs in `voice_wave_bits[voice_idx]` rather than one global.

`parse_waveform_spec("pulse+triangle")` returns the OR'd bits plus a canonical name; `_PC_WAVEFORMS` and `_WAVEFORM_CYCLE` entries are canonical. The host emulator renders combos faithfully via `sidemu.voice_samples`' 12-bit bitwise AND, so the scope matches the chip's combined wave.

**Three control surfaces**, all opt-in — the default is a shared single-channel pool:

1. **Static** — `midi_voice_waveforms`.
2. **SHIFT** (`cycle_style`) — advances *every* voice one step through `_WAVEFORM_CYCLE`: the four singles, then `pulse+triangle`. Per-voice offsets are kept, so a uniform single-bit set cycles in lockstep while a mixed set keeps its relative spread. `_set_voice_waveform` re-emits per voice, and off-cycle combos advance from their dominant single's slot.
3. **Program Change** (`_program_change`, gated by `midi_program_change`) → waveform. Shared mode sets all voices; multitimbral sets the message's channel.

> `pulse+triangle` is the only combined waveform that reliably sounds on a 6581 — sawtooth and noise combos AND down to near-silence on real hardware, so they stay out of the interactive rotation. `midi_voice_waveforms` can still set them explicitly.

**Multitimbral mode** (`midi_voice_mode = "multitimbral"`): `_handle_msg` routes by `msg.channel` through `_chan_to_voice`, built from `midi_voice_channels`, to `_note_on_mt`/`_note_off_mt`. Each voice is monophonic with its own held-note LIFO, and unmapped channels are ignored. Per-voice ADSR, PW, and filter stay global — deferred.

**Instrument controls.** Velocity maps to loudness: `_program_voice` writes velocity into that voice's **sustain** nibble as `velocity >> 3`.

The CC map:

| CC | Target |
| --- | --- |
| 1 | Pulse width, mapped to an audible `[128, 3968]` window so wheel-to-zero doesn't mute |
| 7 | Master volume |
| 74 | Filter cutoff |
| 71 | Resonance |
| 73 / 75 / 72 | Attack / decay / release |

The filter is audible because `_program_global_sid` routes all three voices through it via `$D417`'s low 3 bits — route none and CC74 sweeps a filter no voice passes through. Cutoff defaults open, so a lowpass patch is neutral until swept. `$D418` writes always carry the filter-mode nibble, so a CC7 volume change can't clobber it.

The key difference from `WaveformScene`: **no py65 host emulator.** MidiScene *is* the writer — it computes every SID byte it sends — so it keeps a 25-byte `$D400-$D418` register **shadow** (`_sid_shadow`, indexed by `addr - $D400`) updated alongside every SID write and feeds `SIDEmulator.update_registers(...)` directly under `_reg_lock`. A re-gate of an already-gated voice (re-trigger / voice steal) shows no off→on edge to `update_registers`, so `_program_voice` passes a per-voice `retrigger` mask to force a hard re-attack (else a plucked sustain=0 voice would flatline after one decay). A background `PollThread` (`midi-env`) advances the ADSR envelopes at the video rate (60/50 Hz) so attack/decay/release tails evolve on screen between MIDI events; render phase is owned by the emulator (advanced by `voice_samples` during render), same as WaveformScene.

Display is fixed bank-0 hires (no relocation — MidiScene uploads no SID payload and leaves the audio ring idle). Bitmap-only ⇒ `_validate_midi` reports a `hires` display so PETSCII overlays are rejected (same as waveform). **Voice strips show activity by color**: `process_frame` change-detects each voice's sounding state (gated or envelope > eps) and repaints its strip color — its configured/per-waveform color while sounding, **gray when idle** (a released voice's flat trace then reads as "off", which is why no per-voice note text is needed). The two bottom text rows are change-detected (repainted only on note/CC events): row 22 = per-voice waveform tags + `VOL nn` (`_build_title_line` via `_abbrev_waveform`: `1:PUL 2:SAW 3:NOI`, combos as `P+T`); row 23 = a live controller readout (`PW nn%  CUT … RES … A. D. R.`). `target_fps` defaults to half the video rate (30/25), like WaveformScene; the scope knobs (`color_mode`/`time_base`/`auto_cycles`/`persistence`/`scroll_columns`/`voice_colors`/`waveform_colors`) all apply.

## `asid.py` + `asid_scene.py` — AsidScene (ASID client → real SID + oscilloscope)

**Framing.** `AsidScene` makes c64cast an ASID **client**: any ASID *host* (DeepSID in a browser, SIDFactory II, Plogue chipsynth C64, Elektron ASID-XP) streams packed SID register writes over MIDI SysEx, and c64cast plays them on the **real SID chip** (U64/TeensyROM) with the shared 3-voice oscilloscope on HDMI. It is a **new input path, not an audio-fidelity change** — ASID carries only SID-synthesizable chip music (never arbitrary/PCM audio; the spec forbids digi and its ≈50-75 Hz frame rate can't reach the DAC's ≈13-14 kHz sample rate), so it does **not** replace the sampled DAC / FPGA-sampler path and is orthogonal to the bus-halt wobble (that wobble lives on the PCM ring path, already solved on the U64 by the off-bus sampler; SID tunes already have a wobble-free resident-player path). See the [ASID spec](https://github.com/thomasj/asid-protocol).

### `asid.py` — the pure decoder
 No mido, no hardware, so it's unit-tested by feeding byte sequences (`tests/test_asid.py`). `decode(data)` takes a SysEx payload (mido's `msg.data` = the bytes between `F0`/`F7`, starting with the `0x2D` manufacturer id), validates the id, dispatches on the command byte, and returns an `AsidUpdate` (or `None` for foreign SysEx). Honored: `0x4E` register data (the workhorse — SID chip 0), **the multi-SID streams `0x50-0x5F` (SID2..SID17 = chips 1..16, same packed format)**, `0x4C`/`0x4D` start/stop, `0x4F` character display, `0x31` speed (PAL/NTSC + multiplier + frame delta + a **buffering-requested** bit in `buffering_requested`), `0x32` SID type (6581/8580, per chip), and **the `0x30` timing recipe** — decoded into `timing_recipe`, an ordered `(asid_reg_id, wait_cycles)` list (`data0` bits 0-5 = register index in write order; `data0` bit 6 + `data1` bits 0-6 = the 0..255 cycle delay after that write). Only `0x60` OPL-FM stays `dropped=True` (no OPL); the buffered ring player below honors the recipe, the coalesced path ignores it (a whole-image block write needs no per-register order). Every register/type update carries a `chip_index` (0 for `0x4E`, `cmd - 0x50 + 1` for the multi-SID commands, `data0` for `0x32`) so the scene routes it to the matching SID address. The `0x4E` unpack reconstructs each present register's 8-bit value (7 low bits from `register_data`, 8th from the packed MSB bytes) and maps the ASID register ID → SID offset via `_ASID_REG_TO_OFFSET` (keyed by offset `addr - $D400`, so it doubles as a shadow index). **Double control write:** ASID orders the three voice control registers last (IDs 22-27) so a frame can carry a *second* write to each control reg — the gate-off→gate-on hard-restart trick. The decoder keeps the second (final) value in `regs` and surfaces the first in `control_first` for the scene's two-phase emit.

### `asid_scene.py` — the scene
 `AsidScene(VoiceScopeRenderer, Scene)`, `WANTS_AUDIO_LOCK = True`, is the sibling of `MidiScene` (see below): same VoiceScopeRenderer visualization, same MIDI-port plumbing, a 25-byte `$D400-$D418` shadow + `SIDEmulator` **per chip**. The difference is the input — MidiScene *synthesizes* SID writes from notes/CCs, AsidScene *relays* the finished register bytes — so all synth knobs (waveform/ADSR/filter/voice allocation) are gone; ASID carries that state. It has **two playback paths**, selected by `asid_buffered_player` (auto/on/off): the default *coalesced* path and the *buffered* ring player (next subsection). In the **coalesced** path the reader thread drains SysEx into the per-chip shadows and flushes a hardware block write at `_FLUSH_INTERVAL_S` (≤60 Hz) so a burst or high-multispeed tune can't outrun the ≈200 writes/s DMA ceiling (high multispeed is frame-decimated — this is the limitation the buffered path removes). Each flush is one `write_regs(<chip base>, *shadow)` block write **per dirty chip**; a within-frame hard restart first emits the `control_first` values as individual `write_memory` calls *before* that chip's block (so the gate-off pulse reaches the chip), passing a `retrigger` mask to `update_registers`. Bitmap-only ⇒ `_validate_asid` reports a `hires` display (PETSCII overlays rejected). Info rows: title = name + SID count + play state + SID type; meta = the host's `0x4F` "now playing" text if any, else a per-voice waveform + `VOL nn` summary of the primary chip. `0x31` PAL/NTSC switches every emulator clock live. Teardown stops the ring player first (if buffered), then silences every mapped SID, restores the SID-address config (below), and restores bank-0 + default `$D018`, like MidiScene.

### `asid_player.py` — buffered C64-side ring player

Gives cycle-accurate multispeed.

**The problem.** The coalesced path is host-driven, so multispeed tunes (`0x31` up to 16×) drop the intermediate frames between flushes — arps, vibrato, and hard restarts all mangled — and every flush jitters.

**The approach.** Move frame *consumption* onto the C64. U64 only, since it needs a bus-clean `reu_write`. `asid_buffered_player` `"auto"` engages it when `profile.supports_reu`; `"on"` forces it, warning and falling back without an REU; `"off"` always coalesces.

**Producer side (host).** The reader groups the stream by frame — every `0x50-0x5F` between two chip-0 `0x4E` messages, emitted on the next `0x4E`, start, or stop, costing one frame of latency. It serializes each active chip's *frame deltas* into a fixed-size **slot** (`serialize_frame` + `pack_slot`), with the absolute SID address baked in host-side from `_chip_addresses` so the 6502 stays chip-agnostic, then `push_frame`s it to `AsidRingPlayer`.

The player REUWRITEs slots into a ring in REU SDRAM at `RING_BASE = $300000`, clear of the mic, sampler, and video regions, ahead of a **computed read head**:

```
read_head = floor((monotonic − gate) · rate)
```

The C64 crystal is exact, so this is fully open-loop: no servo, and **no C64→host reads during playback**, which satisfies the "no rapid U64 reads during capture" rule.

**Consumer side (6502).** A handler at `$C000` — the whole page is free, since AsidScene runs no `$D418` DAC or NMI — is fired by **CIA #1 Timer A** at the ASID cadence and pops one slot per tick:

1. Pull the slot REU→landing-buffer (`$C400`) via a main-RAM src tracker at `$C800`, wrapping at ring end. As with the tracked audio pump, it never trusts the REU read-back.
2. Execute its ops — `[n_ops]`, then `[addr_lo][addr_hi][value][wait]` × n — self-modifying a `STA` target per op and busy-waiting `wait` units of ≈`DELAY_CYCLES_PER_UNIT` cycles each. That reproduces the `0x30` recipe's inter-write timing and hard-restart order.

`n_ops == 0` is a **hold** tick: the SID holds state, which is the graceful underrun pad, with no echo. A tick divider chains `$EA31` every Nth tick so SCNKEY and jiffy stay ≈60 Hz.

**Bring-up arms lazily** — this is hardware-driven, and both symptoms below were observed. `start` prefills holds, uploads the handler, seeds the tracker, programs the CIA #1 latch, and starts the writer thread. But it does **not** swap `$0314` or start the read-head clock until a real-frame prebuffer has accumulated (`_try_arm`).

That matters because the producer is real-time: an ASID host does not begin streaming the instant we install.

* **Symptom 1 — arming eagerly.** The computed read head runs away during the startup gap and lands real frames in already-consumed ring slots. Heard as unbroken holds. Arming when the prebuffer is full instead makes `gate_time` coincide with data flowing, so the write head stays a full `lead` ahead.
* **Symptom 2 — ignoring `0x31` before arming.** `0x31` retunes the CIA latch and re-anchors the cumulative read head, so absolute-slot alignment survives a rate change. It must apply **before arming too**, because a `0x31` almost always arrives at stream start, before the prebuffer fills. Dropping it arms at the wrong video-rate cadence and silently decimates the tune.

A chip-count change `reinit`s the player with a new slot size. Teardown restores `$0314` → `$EA31` and the kernal CIA #1 latch.

Because the producer feeds in real time at exactly the consume cadence, the ring's lead can never *grow* past the startup prebuffer — so the prebuffer is seeded to the full lead for maximum jitter cushion, and a genuine producer stall pads a hold (SID holds its last state — no echo). HW-verified on the U64-II: at a 2× multispeed the buffered player's audio modulates at exactly 2× the coalesced path's rate (measured by spectral flux), i.e. it plays every frame where the coalesced path decimates to ≈60 Hz. The pure builders (`serialize_frame`/`pack_slot`/`slot_size_for_chips`/`build_player` + the CIA-latch helpers) and the ring math are unit-tested in `tests/test_asid_player.py`. v1 limitations (documented in caveats): a frame-fit ceiling (per-frame op cost + waits summed across chips must fit the frame period) and coarse cycle delay.

### Multi-SID on the U64 (`asid_sidmap.py`)
 When the stream reveals a chip index > 0 and the backend exposes the config API (`profile.supports_config` — Ultimate only), the scene honors real multi-SID. The **pure planner** `plan_sid_map(n_sids, socket1_present, socket2_present)` decides the U64 address map: it prefers **physical socket SIDs** (chips 0-1 → sockets at `$D400`/`$D420` when detected), then fills the tail from the two UltiSID FPGA cores using the minimal address-line split (`Off`/`1/2`/`1/4`, up to 8 total). The cores sit on the `$D400` page when no sockets are used (chip 0 stays at `$D400`) or the `$D5xx` page when sockets occupy `$D4xx` — the firmware force-aligns a split core's base (`1/2`→`$40`, `1/4`→`$80`), so the `$5xx` page keeps cores clear of the sockets at any split. `Auto Address Mirroring` is disabled so each base responds distinctly. The planner is unit-tested against a Python port of the firmware address math (`u64_offsets`/`split_bits`/`fix_splits`) that asserts the realized instances are distinct and cover the routed addresses (`tests/test_asid_sidmap.py`). Chip count is **detected dynamically**: the reader tracks the max chip index seen; `process_frame` (main thread, to keep display mutation off the reader) grows the map — snapshots the current SID-address config once (via `api.get_config_category`, restored on teardown), applies the new map live (`api.put_config_item`, no reboot / not flashed → reverts on power-cycle), updates routing, and reflows the split scope. The **scope subdivides each of the three voice rows horizontally**, one cell-aligned window per chip (`voice_scope._compute_window_slices`), a 1px gutter between chips; a single chip takes the full row, so waveform/midi render exactly as they do without the split. `asid_multi_sid` (default true) and `asid_max_sids` (1-8) gate/cap it; on TeensyROM or when disabled, extra chips downmix to the primary SID with a one-time warning. See docs/caveats.md.

**Two planners, one policy.** `plan_sid_map` above is the *canonical* layout — it chooses its own addresses and knows only which sockets are populated, which is all an ASID stream can tell it (no chip-model information rides the protocol). `plan_sid_map_for_addresses` realizes a `.sid` file's **own** fixed bases and additionally takes `required_models` + `socket_models`, so a socket claims a target only when it carries the model that chip asked for. That is what lets routing and model matching happen in one pass — see [SID Player Autoconfig](#sid-player-autoconfig) for why they cannot be split into two.

Both share the rest of the policy:

* **Unclaimed sockets are explicitly `Disabled`.** A socket left enabled at an address the plan just handed to an UltiSID core answers alongside it, and the tune plays on both chips at once. The disable is unconditional rather than gated on chip detection, because detection is exactly what can't be trusted when a stale config is the problem.
* **Spare cores mirror the socket addresses** rather than being unmapped, so the LED display still lights — see [LED mirroring of socketed SIDs](#led-mirroring-of-socketed-sids).
* **Cores beyond that are `Unmapped`**, so a stale mapping from a previous run can't answer an address this plan didn't intend.

The oracle in `tests/test_asid_sidmap.py` asserts each routed chip is realized *by the source the map says plays it*, and that the only address answered by two sources is a deliberate LED mirror.

**Follow-ups (see auto-memory):** the reverse direction — c64cast as an ASID *host* emitting `0x4E` from the `sid_host_emu` register capture to drive external ASID synths; and per-chip color tint / labels on the split scope (v1 distinguishes chips by position + gutter only).
