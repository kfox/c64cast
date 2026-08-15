---
number: A
generated: true
---

# Configuration Sections

Every section of a configuration file, in alphabetical order: 20 sections and 169 fields, with the type each takes and the value it holds when you say nothing. A field a knob can move mid-show says so, and names the target Appendix F lists it under. Each section opens with a fragment showing how it is written; the table under it is the whole section. `c64cast --describe section:NAME` prints any one of these at the terminal.

## `[audio]`

SID audio streaming.

```toml
[audio]
enabled = true
device = -1
sample_rate = 12000
backend = "auto"     # auto | dac | sampler
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `True` | Master switch for SID audio streaming (the 4-bit $D418 DAC). On by default; mute with the `--no-audio` CLI flag. |
| **`device`**<br>*Type:* `int \| str`<br>*Default:* `-1` | Audio input device: an integer index (-1 = system default microphone), or a string matched to an input device by name substring (e.g. "Cam Link"). Run `--list-devices` to see names + indices. |
| **`sample_rate`**<br>*Type:* `int`<br>*Default:* `12000` | Audio sample rate in Hz fed to the SID DAC. Default 12000 lifts the Nyquist to ~6.0 kHz so fricatives/sibilants survive (8000 lost them). HW-verified clean on a real NTSC U64-II via a pitch A/B sweep (no NMI handler overrun) in both char and bitmap modes, and safe on PAL. Note the REAL streaming ceiling sits BELOW the isolated-handler ceiling (max_safe_sample_rate ~13.6 kHz NTSC): the host-DMA audio ring writes themselves halt the 6510 and steal cycles from the NMI handler, so the overrun onset under the live pipeline was measured at ~12500 Hz (identical in char and bitmap — the audio feed, not the video, is the driver). 12000 keeps margin below that. Rates past the isolated-handler ceiling are rejected at load, and `--doctor` reports them. Sampler-backend playback uses [audio].sampler_sample_rate instead. |
| **`backend`**<br>*Type:* `str`<br>*Default:* `'auto'` | Video-audio backend: 'auto' (sampler on a capable U64, else DAC), 'dac' (4-bit $D418 NMI DAC, all backends, lo-fi), or 'sampler' (U64 'Ultimate Audio' FPGA PCM, high fidelity, off the C64 bus). Choices: `auto`, `dac`, `sampler`. |
| **`sampler_sample_rate`**<br>*Type:* `int`<br>*Default:* `44100` | Sample rate (Hz) for the Ultimate Audio sampler backend. 1000..48000; default 44100 (CD quality). The FPGA plays at the nearest divider of its 6.25 MHz reference (a <0.5% constant pitch offset, drift-free). |
| **`sampler_bits`**<br>*Type:* `int`<br>*Default:* `16` | PCM bit depth for the Ultimate Audio sampler backend: 8 (signed) or 16 (signed little-endian). Default 16. |
| **`sampler_clock_hz`**<br>*Type:* `int`<br>*Default:* `6160000` | Ultimate Audio sampler reference clock (Hz), used to derive the rate divider AND the resample target so they stay matched (heard speed = real_clock / this). Default is the MEASURED effective clock of the shipping U64 firmware (~6160000 Hz): the FPGA runs ~1.44% slow vs the 6250000 Hz design nominal, so nominal made sampler audio drift against video. This is a firmware property (same across U64 units), not per-unit — so it ships baked in. If a firmware update fixes the clock (or on hardware that clocks it correctly), set 6250000. The repository carries a diagnostic script that re-measures it and prints the value. Only affects the sampler backend. |
| **`mic_sensitivity`**<br>*Type:* `float`<br>*Default:* `1.5` | Microphone input gain multiplier. |
| **`noise_gate`**<br>*Type:* `float`<br>*Default:* `0.05` | Mic level below which input is squelched to silence. |
| **`dither`**<br>*Type:* `bool`<br>*Default:* `False` | TPDF dither on the 4-bit quantization step. Default off; flip on for smoother hiss on already-noisy sources. |
| **`digi_boost`**<br>*Type:* `bool`<br>*Default:* `False` | EXPERIMENTAL: lock SID voices to a DC pulse so the ADSR D/As bias the master mixer, raising $D418 playback level. |
| **`dac_curve`**<br>*Type:* `str`<br>*Default:* `'auto'` | SID $D418 DAC companding curve. 'auto' (default) = calibrated table for the SID answering $D400 if present, else 'mahoney_ultisid' when an UltiSID core owns $D400, else 'linear' (an uncalibrated physical chip — run `--calibrate-dac` to measure it). 'linear' = classic 4-bit volume nibble. 'mahoney_ultisid' = Mahoney 8-bit technique (full $D418 byte, ~6-7 effective bits) with the baked emulated-UltiSID table. 'calibrated' = this system's per-unit table from `--calibrate-dac` (errors if none). Non-linear curves require the Mahoney SID env (auto-installed) and are mutually exclusive with digi_boost. Choices: `auto`, `linear`, `mahoney_ultisid`, `calibrated`. |
| **`dac_calibration_profile`**<br>*Type:* `str \| None`<br>*Default:* `None` | Override the auto-derived calibration file key (device unique_id / TR USB serial) with a name — calibration/dac/profile-<name>.json, or an existing file's own name (e.g. the device-keyed 'ultimate-<id>' that `--calibrate-dac` writes), used as-is — or with a path to a calibration file, used as given. Use when a TeensyROM+ moves between physical C64s (name each host's calibration once at `--calibrate-dac` time, then pass the same name on every playback run against that host), or to reuse one machine's calibration from another backend (a path, since that file is keyed by the other backend's device identity). |
| **`sid_filter_cutoff`**<br>*Type:* `int`<br>*Default:* `0` | SID low-pass cutoff for the PWM carrier voice (0 = disabled). Attenuates the carrier above the audio band. |
| **`use_reu_pump`**<br>*Type:* `bool`<br>*Default:* `False` | EXPERIMENTAL: stream video/mic audio from a REU ring (bus-clean) instead of per-write host DMA. Requires REU enabled. |
| **`reu_pump_governor`**<br>*Type:* `bool`<br>*Default:* `True` | C64-side rate governor for the REU audio pump: the pump IRQ skips a chunk when its write head outruns the reader, stopping drift/echo with no host writes. Only active with use_reu_pump. |
| **`host_dma_servo`**<br>*Type:* `bool`<br>*Default:* `True` | Closed-loop pacing for the host-DMA audio worker (mic / videos): reads the C64 NMI read pointer and adjusts the producer's software pace so the ring write head holds a fixed gap behind the reader, stopping the ~26s drift/echo. Pure host-side timing, no C64 writes. Not the REU pump path. |
| **`nmi_rate_adaptive`**<br>*Type:* `bool`<br>*Default:* `False` | Adaptive NMI-rate compensation: closed-loop on the measured C64 consumer rate, raises the NMI rate to cancel a video slowdown from bus-halt-stolen NMI ticks. DEFAULT OFF — modern fps caps + REU-staged double-buffer drove that loss to ~0, so this only adds pitch error now. Supersedes pitch_mult_* when on. Host-DMA path only. |
| **`source_alignment_marker`**<br>*Type:* `bool`<br>*Default:* `False` | DEBUG/CAPTURE ONLY: prepend a 100 ms chirp to REU audio as a capture-alignment anchor. Turn OFF for production listening. |
| **`pitch_mult_petscii`**<br>*Type:* `float`<br>*Default:* `1.0` | Host-DMA servo playback-rate multiplier for PETSCII mode (light char-mode load). 1.0 = none (default; U64-II NTSC is dead-on). Quantized: the NMI period is an integer cycle count, so a request rounds onto the latch grid (~1.2% steps at 12 kHz) — 1.005 is a no-op, 1.015 lands on +1.19%. |
| **`pitch_mult_hires`**<br>*Type:* `float`<br>*Default:* `1.0` | Host-DMA servo playback-rate multiplier for Hires / Hires-edges modes. 1.0 = none (default; modern fps caps + REU staging leave ~0 loss on U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts. Quantized: the NMI period is an integer cycle count, so a request rounds onto the latch grid (~1.2% steps at 12 kHz) — 1.005 is a no-op, 1.015 lands on +1.19%. |
| **`pitch_mult_mhires`**<br>*Type:* `float`<br>*Default:* `1.0` | Host-DMA servo playback-rate multiplier for MultiHires mode. 1.0 = none (default; modern fps caps + REU staging leave ~0 loss on U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts. Quantized: the NMI period is an integer cycle count, so a request rounds onto the latch grid (~1.2% steps at 12 kHz) — 1.005 is a no-op, 1.015 lands on +1.19%. |
| **`pitch_mult_mcm`**<br>*Type:* `float`<br>*Default:* `1.0` | Host-DMA servo playback-rate multiplier for MCM mode (char-based, light load; U64-II NTSC: good at 1.0). Quantized: the NMI period is an integer cycle count, so a request rounds onto the latch grid (~1.2% steps at 12 kHz) — 1.005 is a no-op, 1.015 lands on +1.19%. |
| **`pitch_mult_blank`**<br>*Type:* `float`<br>*Default:* `1.0` | Host-DMA servo playback-rate multiplier for Blank mode (no video input; 1.0 = none). Quantized: the NMI period is an integer cycle count, so a request rounds onto the latch grid (~1.2% steps at 12 kHz) — 1.005 is a no-op, 1.015 lands on +1.19%. |
| **`dac_bitmap_tempo_hires`**<br>*Type:* `float`<br>*Default:* `0.89` | Observed $D418-DAC playback-speed fraction on Hires / Hires-edges bitmap modes (measure via clock/wall). Content is time-compressed by 1/value (pitch-preserving) so bitmap+DAC video plays at real time. 1.0 = off. Host-DMA DAC path only — no effect on the Ultimate Audio sampler or the REU pump. Default 0.89 = U64-II NTSC (Hires drains slightly faster than MHires); re-measure per platform (PAL / TR+). |
| **`dac_bitmap_tempo_mhires`**<br>*Type:* `float`<br>*Default:* `0.88` | Observed $D418-DAC playback-speed fraction on MultiHires bitmap mode (measure via clock/wall). Content is time-compressed by 1/value (pitch-preserving) so bitmap+DAC video plays at real time. 1.0 = off. Host-DMA DAC path only — no effect on the Ultimate Audio sampler or the REU pump. Default 0.88 = U64-II NTSC; re-measure per platform (PAL / TR+). |

## `[audio_features]`

Analyzer that turns live audio input into reactive-visual features (level / bands / transients / tempo) for a generative scene with audio_source = 'mic' and reactive = true.

```toml
[audio_features]
bands = 8
onset_sensitivity = 1
poll_hz = 60
fft_size = 1024
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`bands`**<br>*Type:* `int`<br>*Default:* `8` | Number of log-spaced frequency bands the analyzer reports (low→high). Generators fold these into bass/mid/treble thirds, so multiples of 3 are not required; 8 matches the spectrum_petscii overlay's bands. More bands = finer spectral detail, no meaningful cost. |
| **`onset_sensitivity`**<br>*Type:* `float`<br>*Default:* `1.0` | Transient-detection sensitivity. The spectral-flux threshold is divided by this, so >1 fires onsets more readily (sparse/soft material, a quiet feed) and <1 fires less (dense or heavily compressed material where everything reads as a transient). 1.0 is the tuned default. |
| **`poll_hz`**<br>*Type:* `float`<br>*Default:* `60.0` | Analysis rate in Hz. 60 matches a full-rate display, so every rendered frame sees fresh features. Lower it only to save host CPU; below ~30 transients start to smear. |
| **`fft_size`**<br>*Type:* `int`<br>*Default:* `1024` | Analysis window in samples. Larger = finer frequency resolution but blurrier transient timing; 1024 is the balance point at the DAC's sample rates. |
| **`listen_sample_rate`**<br>*Type:* `int`<br>*Default:* `44100` | Capture rate in Hz for audio_source = 'listen' (the listen-only path, which never feeds the DAC and so isn't bound to its ~12 kHz rate). 44100 gives the analyzer full-bandwidth audio — real hi-hat energy above the DAC's 6 kHz Nyquist and cleaner transients. Ignored by audio_source = 'mic' (that path analyzes at the DAC rate, matching what the C64 actually plays). |

## `[color]`

Global pre-quantize color shaping for mcm/mhires/petscii: static channel boost + hue corrections, plus per-source adaptive auto_fit (video/slideshow).

```toml
[color]
hue_corrections_replace_defaults = false
auto_fit = true
auto_fit_strength = 1
force_palette = false
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`channel_boost`**<br>*Type:* `list[float]`<br>*Default:* `[]` | Per-channel pre-quantize gain [blue, green, red] (OpenCV BGR order). Empty = built-in default [1.3, 1.2, 1.0] (blue/green lift toward C64-friendly hues; red left neutral). |
| **`hue_corrections`**<br>*Type:* `list[dict[str, Any]]`<br>*Default:* `[]` | List of [[color.hue_corrections]] bands applied before quantize (keys: hue_lo_deg, hue_hi_deg, sat_thresh, val_thresh, sat_mult, val_mult, hue_target_deg, name). Empty = built-in purple rescue only. |
| **`hue_corrections_replace_defaults`**<br>*Type:* `bool`<br>*Default:* `False` | If true, user hue_corrections REPLACE the built-in defaults instead of extending them. |
| **`auto_fit`**<br>*Type:* `bool`<br>*Default:* `True` | Per-source adaptive color fit for video + slideshow scenes: pre-scan the source and stretch its contrast + saturation to fill the C64 gamut (faithful — hue preserved). Ignored by webcam scenes (can't pre-scan). |
| **`auto_fit_strength`**<br>*Type:* `float`<br>*Default:* `1.0` | Strength of the auto_fit transform, 0..1 (1 = full, 0 = off). Lerps the derived stretch toward identity. *Live-tunable* while a show runs, as `mode.auto_fit_strength` — Appendix F. |
| **`force_palette`**<br>*Type:* `bool`<br>*Default:* `False` | EXTREME forced-palette remap (mcm/mhires): k-means the source into N clusters and map each to a DISTINCT C64 color so all N colors are used. Pre-scanned for video + slideshow; adapts live (rolling, warm-start + hysteresis) for webcam/wled/generative. Deliberate false-color (NOT faithful) — off by default; also reachable via the SHIFT cycle's 'percell+forced' stop once enabled. Tip: `--suggest-palette FILE` ranks a good force_palette_colors set. |
| **`force_palette_colors`**<br>*Type:* `int \| list[int \| str]`<br>*Default:* `16` | How force_palette allocates C64 colors: either an int count of distinct colors to spread the source across (2..16), OR an explicit list of colors to whitelist — each a color name (fuzzy + case-insensitive, e.g. "light blue", "lgrn", "blk") or an index 0..15. A list's length sets the color count. |
| **`dither`**<br>*Type:* `str`<br>*Default:* `'auto'` | Spatial dither applied before nearest-palette quantization on mhires/mcm/hires. 'auto' picks the best method that's actually useful for the scene: floyd-steinberg (highest quality) for static scenes (slideshow), blue_noise (vectorized, temporally stable — no added shimmer, and no Bayer grid structure) for motion scenes (video/webcam/generative). Any value can be forced on any scene; floyd-steinberg/atkinson are a Python-level per-pixel loop and can shimmer frame-to-frame on motion; 'ordered' (Bayer) is the older motion default and still available if the cross-hatch pattern is wanted (see docs/caveats.md). Choices: `auto`, `none`, `ordered`, `blue_noise`, `floyd-steinberg`, `atkinson`. *Live-tunable* while a show runs, as `mode.dither_method` — Appendix F. |
| **`dither_strength`**<br>*Type:* `float`<br>*Default:* `0.5` | Dither strength, roughly 0..2.0. For 'ordered'/'blue_noise' it scales the threshold spread (same scale for both, so switching between them doesn't need a strength retune); for floyd-steinberg/atkinson it scales how much of each pixel's quantization error is diffused to its neighbors (1.0 = the textbook kernel weights). *Live-tunable* while a show runs, as `mode.dither_strength` — Appendix F. |
| **`color_match`**<br>*Type:* `str`<br>*Default:* `'auto'` | Color space for the nearest-palette match on the quantizing modes (mcm/mhires/hires/petscii). 'perceptual' measures nearest-color in CIE-Lab (perceptually uniform — picks the color the eye calls closest, e.g. a warm gray → orange/brown, not muddy gray). 'rgb' is the classic brightness-weighted BGR metric. Both keep the channel_boost + gray-penalty shaping; only the distance space differs. 'auto' (default) picks perceptual on every quantizing mode (a no-op on hires edges / blank, which pick no colors). Choices: `auto`, `rgb`, `perceptual`. *Live-tunable* while a show runs, as `mode.color_match` — Appendix F. |
| **`cell_strategy`**<br>*Type:* `str`<br>*Default:* `'auto'` | How mhires percell mode fills each 4×8 cell's 3 per-cell color slots from the colors present in that cell. 'frequency' = the 3 most-common (temporally stable). 'luminance' = darkest/median/brightest (preserves a cell's full tonal span). 'contrast' = the two luma extremes plus the color farthest from both. 'error-min' = the trio minimizing the cell's reconstruction error (best quality, costlier). 'auto' (default) uses error-min for static scenes (slideshow — composed once) and frequency for motion scenes (video/webcam/generative, where frequency's stability avoids per-frame slot churn). Only affects mhires with palette_mode=percell. Choices: `auto`, `frequency`, `luminance`, `contrast`, `error-min`. *Live-tunable* while a show runs, as `mode.cell_strategy` — Appendix F. |
| **`hires_cell_pick`**<br>*Type:* `str`<br>*Default:* `'error-min'` | How hires picks each 8×8 cell's foreground color against the global background. 'error-min' (default) picks the color minimizing that cell's own reconstruction error. 'sample' reads a single pixel per cell — ~0.8 ms/frame cheaper, but measurably worse on both accuracy and frame-to-frame stability, so it's for tight CPU budgets only. Only affects the hires 'normal' style (the edges styles are fixed 2-color). Choices: `error-min`, `sample`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`motion_smoothing`**<br>*Type:* `float`<br>*Default:* `0.25` | Temporal smoothing for mhires percell mode, 0..1. The percell path smooths its per-cell color choices over time (an EMA over color counts plus per-pixel/per-cell decision hysteresis) to suppress frame-to-frame flicker on noisy video. That smoothing trades motion-tracking for stability, so on a hard shot cut an outline from the previous shot lingers as an after-image for a moment. 1.0 (full smoothing) is the most stable but ghostiest; 0.0 tracks the source exactly (no after-image) but can flicker on grainy content. The default 0.25 was picked by hardware A/B as the best ghost/flicker balance. Lower it if after-images still bother you, raise it if motion shimmers. No effect on other modes or palette_modes. *Live-tunable* while a show runs, as `mode.motion_smoothing` — Appendix F. |

## `[control]`

HTTP control plane (extra).

```toml
[control]
enabled = false
host = "127.0.0.1"
port = 8765
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Run the HTTP control plane (pause/resume/skip/reload); requires the 'control' extra. |
| **`host`**<br>*Type:* `str`<br>*Default:* `'127.0.0.1'` | Bind address for the control-plane HTTP server. |
| **`port`**<br>*Type:* `int`<br>*Default:* `8765` | Bind port for the control-plane HTTP server. |
| **`token`**<br>*Type:* `str`<br>*Default:* `''` | Shared token required on every control-plane request, including the /perf console and its WebSocket. Empty = no authentication (the historical behaviour). Prefer the C64CAST_CONTROL_TOKEN env var. |
| **`viewer_token`**<br>*Type:* `str`<br>*Default:* `''` | Optional second token granting read-only access (GET/HEAD only): the /perf console watches but can't launch. Ignored unless `token` is set. Prefer the C64CAST_CONTROL_VIEWER_TOKEN env var. |

## `[debug]`

Logging, heartbeat, profiling.

```toml
[debug]
verbose = 0
heartbeat = 10
skip_probe = false
profile = false
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`verbose`**<br>*Type:* `int`<br>*Default:* `0` | Log verbosity (0 = INFO; 1+ = DEBUG). CLI: -v / -vv. |
| **`heartbeat`**<br>*Type:* `float`<br>*Default:* `10.0` | Seconds between health heartbeat log lines (0 disables). |
| **`skip_probe`**<br>*Type:* `bool`<br>*Default:* `False` | Skip the startup U64 reachability probe. |
| **`log_file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Also mirror log output to this file (useful for headless runs). |
| **`profile`**<br>*Type:* `bool`<br>*Default:* `False` | Emit per-scene frame-timing summaries (render/compose/push/wait). |
| **`profile_interval`**<br>*Type:* `float`<br>*Default:* `10.0` | Seconds between profiler summary lines. |
| **`frame_numbers`**<br>*Type:* `bool`<br>*Default:* `False` | Overlay the playback timecode + source frame number on video/slideshow/webcam frames (debug aid for locating flashing/flickering frames). |

## `[dsp]`

Host-side audio DSP before the 4-bit DAC: compressor/limiter, expander (replaces the hard gate), pre-emphasis, and mic AGC.

```toml
[dsp]
enabled = true
expander = true
expander_threshold_db = -45
expander_ratio = 2
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `True` | Master switch for the host-side audio DSP chain (ON by default — the 4-bit DAC needs it). Set false for the legacy linear encode + hard mic gate. |
| **`pre_emphasis`**<br>*Type:* `float \| None`<br>*Default:* `None` | High-frequency boost amount; y[n]=x+amt*(x-x[-1]). Brightens speech for intelligibility. Unset = source-aware default (mic 0.7 / line 0.6); a number forces that amount for all sources; 0 disables. Per-scene [[scenes]].pre_emphasis overrides this. |
| **`expander`**<br>*Type:* `bool`<br>*Default:* `True` | Downward expander with hysteresis (replaces the hard noise gate when DSP is enabled). Attenuates below the threshold. |
| **`expander_threshold_db`**<br>*Type:* `float`<br>*Default:* `-45.0` | Level below which the expander attenuates (dBFS). |
| **`expander_ratio`**<br>*Type:* `float`<br>*Default:* `2.0` | Expansion ratio (>1; larger = more attenuation below thresh). |
| **`expander_hysteresis_db`**<br>*Type:* `float`<br>*Default:* `6.0` | Gap (dB) below the open threshold before the gate closes — prevents chatter on signal hovering at the threshold. |
| **`expander_floor_db`**<br>*Type:* `float`<br>*Default:* `-60.0` | Maximum attenuation the expander applies (dB). |
| **`expander_attack_ms`**<br>*Type:* `float`<br>*Default:* `5.0` | Expander gain open (attack) time constant in ms. |
| **`expander_release_ms`**<br>*Type:* `float`<br>*Default:* `80.0` | Expander gain close (release) time constant in ms. |
| **`compress`**<br>*Type:* `bool`<br>*Default:* `True` | Soft-knee feed-forward compressor + makeup gain — the main win for fitting program dynamics into 4 bits. |
| **`comp_threshold_db`**<br>*Type:* `float`<br>*Default:* `-18.0` | Compression threshold (dBFS); above this, gain reduces. |
| **`comp_ratio`**<br>*Type:* `float`<br>*Default:* `3.0` | Compression ratio (>=1; e.g. 3 = 3:1 above threshold). |
| **`comp_knee_db`**<br>*Type:* `float`<br>*Default:* `6.0` | Soft-knee width in dB around the threshold (0 = hard knee). |
| **`comp_attack_ms`**<br>*Type:* `float`<br>*Default:* `5.0` | Compressor attack time constant in ms. |
| **`comp_release_ms`**<br>*Type:* `float`<br>*Default:* `120.0` | Compressor release time constant in ms. |
| **`comp_makeup_auto`**<br>*Type:* `bool`<br>*Default:* `True` | Auto-compute makeup gain so threshold-level signal exits near unity. Set false to use comp_makeup_db explicitly. |
| **`comp_makeup_db`**<br>*Type:* `float`<br>*Default:* `0.0` | Explicit makeup gain (dB) when comp_makeup_auto is false. |
| **`limiter`**<br>*Type:* `bool`<br>*Default:* `True` | Fast peak limiter / brickwall ceiling — final safety stage. |
| **`limiter_ceiling`**<br>*Type:* `float`<br>*Default:* `0.95` | Limiter output ceiling, linear 0..1 (just under full scale). |
| **`limiter_release_ms`**<br>*Type:* `float`<br>*Default:* `50.0` | Limiter gain recovery (release) time constant in ms. |
| **`agc`**<br>*Type:* `bool`<br>*Default:* `False` | Automatic gain control for the MIC path only (line/video audio is already peak-normalized). Slow gain toward a target. EXPERIMENTAL: being level-based it can boost a sustained noise floor during long pauses — best on clean mics, or pair with the expander / raise agc_noise_floor_db above the floor. |
| **`agc_target_db`**<br>*Type:* `float`<br>*Default:* `-18.0` | AGC target RMS level (dBFS). |
| **`agc_max_gain_db`**<br>*Type:* `float`<br>*Default:* `24.0` | Maximum AGC gain/attenuation magnitude (dB). |
| **`agc_time_ms`**<br>*Type:* `float`<br>*Default:* `300.0` | AGC adaptation time constant in ms (larger = slower/steadier). |
| **`agc_noise_floor_db`**<br>*Type:* `float`<br>*Default:* `-60.0` | Below this input RMS, AGC holds gain instead of amplifying the noise floor. |

## `[hardware]`

Hardware backend selection.

```toml
[hardware]
backend = "ultimate"         # ultimate | teensyrom
host_sid_model = "auto"      # auto | 6581 | 8580 | unknown
host_sid_tune_match = "off"  # off | prefer | require
host_palette = "auto"
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`backend`**<br>*Type:* `str`<br>*Default:* `'ultimate'` | Hardware backend family driving the C64. Choices: `ultimate`, `teensyrom`. |
| **`host_sid_model`**<br>*Type:* `str`<br>*Default:* `'auto'` | SID chip model in the C64 being driven, so a tune asking for the other model still gets a warning on links that can't read the SID hardware state (e.g. TeensyROM). 'auto' assumes 6581 on NTSC / 8580 on PAL and logs that assumption; 'unknown' opts out of model-match verdicts. Ignored where the live SID state is readable (U64). Choices: `auto`, `6581`, `8580`, `unknown`. |
| **`host_sid_chips`**<br>*Type:* `dict[str, str]`<br>*Default:* `{}` | Internal SID chips in the C64 being driven, as address=model (e.g. d400='6581', d420='8580') — for machines with a dual-SID mod, whose second chip host_sid_model can't describe. When set it supersedes host_sid_model, so no NTSC/PAL assumption is made. Ignored where the live SID state is readable (U64). |
| **`host_sid_tune_match`**<br>*Type:* `str`<br>*Default:* `'off'` | Bias a multi-file waveform pool toward tunes the C64's own SID chips can play as authored (right model, and a chip at every address the tune drives). 'prefer' tries fitting tunes first but falls back to the rest; 'require' skips non-fitting tunes outright. Needs host_sid_chips or an explicit host_sid_model — an assumed model is never acted on. Ignored where the live SID state is readable (U64), which re-places chips per tune instead. Choices: `off`, `prefer`, `require`. |
| **`host_palette`**<br>*Type:* `str`<br>*Default:* `'auto'` | The 16 colors the C64 being driven actually emits, which the quantizer aims at. 'auto' (default) reads it from the machine where it can — an Ultimate 64 reports its own palette — and otherwise assumes a real VIC-II. 'u64' is the Ultimate 64's own table; 'pepto' is the classic VIC-II rendering, right for a real C64 (so for an Ultimate II+, and for a TeensyROM+ in a breadbin). Can also be the path to a VICE .vpl file, which is how to describe a machine with a custom palette loaded. |
| **`dump_char_rom`**<br>*Type:* `bool`<br>*Default:* `True` | On the first run against a machine, read its character ROM and cache it, so C64 text renders in the real C64 font instead of a built-in ASCII substitute. One ~1s step, never repeated; set false to skip it entirely. |

## `[interstitial]`

The 'UP NEXT' card shown between scenes.

```toml
[interstitial]
duration_s = 4
text_color = "rainbow"
background = "random"
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`duration_s`**<br>*Type:* `float`<br>*Default:* `4.0` | How long the 'UP NEXT' interstitial shows between scenes. |
| **`text_color`**<br>*Type:* `str`<br>*Default:* `'rainbow'` | Interstitial text color: a C64 color name, 'rainbow', or 'random'. |
| **`background`**<br>*Type:* `str`<br>*Default:* `'random'` | Animated parallax background style behind the interstitial text. Choices: `starfield`, `petscii_bars`, `raster_bars`, `checker`, `nature`, `city`, `none`, `random`. |

## `[menu]`

On-C64 SPACE-key menu for live scene tweaks.

```toml
[menu]
enabled = false
prompt_to_save = true
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Enable the on-C64 SPACE-key menu for live scene tweaks. |
| **`prompt_to_save`**<br>*Type:* `bool`<br>*Default:* `True` | On menu exit with unsaved changes, offer to write them back to the source config file. False = apply to the running scene only, never persist (handy for conventions/demos). |

## `[midi_control]`

MIDI CC control surface for live performance: scene jumps, style cycling, transport, live effect params (extra).

```toml
[midi_control]
enabled = false
broadcast_channel = 16
jump_transition = "cut"  # cut | interstitial
osd = "bottom"           # bottom | top | off
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Run the MIDI control listener; requires the 'midi' extra. |
| **`port`**<br>*Type:* `str \| None`<br>*Default:* `None` | MIDI input port name (substring match, case-insensitive). None = first available port. |
| **`broadcast_channel`**<br>*Type:* `int`<br>*Default:* `16` | 1-based MIDI channel that targets every system at once in ensemble mode. Other channels 1..N target the Nth system in ensemble order. Ignored in single-system mode (the one playlist is always the target). |
| **`jump_transition`**<br>*Type:* `str`<br>*Default:* `'cut'` | How a 'jump' action changes scenes: 'cut' (instant, no interstitial — the live-performance default) or 'interstitial' (routes through the normal UP-NEXT card). Choices: `cut`, `interstitial`. |
| **`osd`**<br>*Type:* `str`<br>*Default:* `'bottom'` | On-screen display for live-tune feedback: a brief 'param value' message appears when you sweep a knob or change a mode via MIDI/WLED, then fades. 'top' or 'bottom' picks the corner; 'off' disables it. Rendered pre-quantization so it shows on every display mode (like `--frame-numbers`). Choices: `bottom`, `top`, `off`. |
| **`loop_audio`**<br>*Type:* `str`<br>*Default:* `'on'` | What happens to a video's audio once a transport.* action touches the scene: 'on' (default) keeps audio playing and re-syncs it across every seek/pause/loop splice; 'mute' restores the Phase-2 escape valve (audio mutes for the rest of that scene's run). Falls back to mute behavior automatically when the scene has no audio. The REU-pump audio path is always forced off under transport regardless. Choices: `on`, `mute`. |
| **`cc_map`**<br>*Type:* `list[dict[str, Any]]`<br>*Default:* *40 shipped entries* | MIDI-message -> action mappings ([[midi_control.cc_map]] tables); see `--describe` section:midi_control. Set to [] to disable the shipped defaults, or override/extend individual entries. Each entry: type ('cc'\|'note'\|'pc'\|'mmc'), number (0-127 for cc/note/pc; an MMC command byte — 0x01 stop, 0x02 play, 0x04 FF, 0x05 RW, 0x06 record, 0x09 pause — for mmc), action ('pause'\|'resume'\|'toggle_pause'\|'skip'\|'cycle_style'\|'jump'\|'param'\|'transport.play_pause'\|'transport.stop'\|'transport.loop_toggle'\|'transport.rw'\|'transport.ff'\|'transport.jog'\|'transport.record'\|'loop_slot'); 'jump' also needs an int scene; 'param' also needs a string target ('effect.<name>', 'source.<name>', 'scene.<name>' for scope scenes, or 'mode.<name>' for the display mode's live color knobs — dither_strength/method, motion_smoothing, auto_fit_strength, cell_strategy, color_match, palette_mode). A knob (cc) sweeps a scalar or bucket-selects a choice; a note/pad cycles a choice. The transport.* actions give DJ-style control of a playing video scene (pause-in-place, seek, RW/FF with acceleration while a note is held, an A/B loop, and a rotary jog — 'mode' 'abs'\|'rel', default 'rel'); once touched, that scene's audio follows every seek/pause/loop by default (see loop_audio, and docs/architecture.md's transport note). A 'mmc' entry also matches an MMC transport SysEx from a DAW/controller. 'transport.record' arms a loop (Record -> Stop workflow, red border while armed); 'loop_slot' also needs an int 'slot' >= 1 (a pad number) and recalls that per-video saved loop on a plain press, saves the current loop into it while Stop is held, or clears it while Record is held (note mappings only — an mmc record/stop can't reliably hold for the chord, since MMC has no release event). 'look_save'/'look_recall' (Phase 6) each need an int 'slot' >= 1 — a look captures the active clip + effect-chain state on save and re-fires it on recall. |
| **`controller_profile`**<br>*Type:* `str`<br>*Default:* `'auto'` | Which learned controller profile (from `--midi-setup`) to layer under this config's cc_map. 'auto' (default) loads the stored profile whose learned port name matches the opened MIDI port; a '<name>' loads that named profile (the file stem under the controllers data dir); 'off' ignores profiles entirely. Merge precedence is shipped-defaults < profile < an explicit cc_map here: with no cc_map set, a profile can reclaim the default note/CC assignments; an explicit cc_map (including []) always wins over the profile. Requires [midi_control] to be enabled; needs no extra. |

## `[performance]`

Live-performance tempo/beat grid: follow an external MIDI clock or free-run at a static/tapped BPM (drives launch quantization + tempo-locked effects).

```toml
[performance]
tempo_source = "internal"  # internal | midi | audio
bpm = 120
beats_per_bar = 4
midi_feedback = false
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`tempo_source`**<br>*Type:* `str`<br>*Default:* `'internal'` | Where the beat grid gets its tempo: 'internal' (free-run at `bpm`, with a `tempo_tap` pad for live tapping), 'midi' (follow an external MIDI clock — 0xF8 clock / start / stop / song-position — which arrives via the [midi_control] listener, so enable that too), or 'audio' (lock to the beat the live-input analyzer detects — the audio_source = 'mic'/'listen' scene's reactive tempo drives launch quantize, mod_source='clock' effects and WLED tempo). With 'midi' or 'audio' the grid idles until the first clock byte / detected tempo. Choices: `internal`, `midi`, `audio`. |
| **`bpm`**<br>*Type:* `float`<br>*Default:* `120.0` | Static tempo (beats per minute) for internal drive, and the starting tempo before an external clock is measured. A tap-tempo pad overrides it live. |
| **`beats_per_bar`**<br>*Type:* `int`<br>*Default:* `4` | Beats per bar (the numerator of the time signature) — sets where bar boundaries fall for bar-quantized launches and bar-locked effects. |
| **`clock_port`**<br>*Type:* `str \| None`<br>*Default:* `None` | MIDI input port to read the external clock from when it arrives on a DIFFERENT port than the [midi_control] control surface (substring match, case-insensitive). None (default) = read clock on the same port as control. Only used when tempo_source = 'midi'. |
| **`midi_feedback`**<br>*Type:* `bool`<br>*Default:* `False` | Light a grid controller's pads to show performance state (Live DJ/VJ Phase 4): loaded clip pads dim, the arming pad blinks, the live clip bright, and enabled effect-chain layers lit — all over a MIDI OUTPUT port ([midi_control] must be enabled). The C64 screen stays audience-facing; this replaces on-screen readouts with controller LEDs. The velocity->color convention is per-controller and comes from the learned controller profile's `feedback` block (`--midi-setup` writes it); Launchpad-X palette defaults otherwise. Needs a grid that lights pads from note-on velocity (Novation Launchpad, Akai APC/MPC, Ableton Push); Arturia and other SysEx-only controllers won't light — use the web console for those. |
| **`feedback_port`**<br>*Type:* `str \| None`<br>*Default:* `None` | MIDI OUTPUT port for LED feedback (substring match, case-insensitive). None (default) = the profile's own port, else the same device as the [midi_control] input, else the first output. Only used when midi_feedback = true. |
| **`clips`**<br>*Type:* `list[dict[str, Any]]`<br>*Default:* `[]` | Clip-launch grid ([[performance.clips]], Live DJ/VJ Phase 2): each table is a scene spec (any [[scenes]] field — type, file, source, display, name, duration_s, effect …) plus launch semantics: `slot` (1-based id, unique), `pad`/`pad_type` (the note/PC number that fires it, auto-mapped when [midi_control] is on), `launch` ("trigger"\|"gate"\|"toggle"), `quantize` ("off"\|"beat"\|"bar" — align the swap to the beat grid), and `loop` (repeat until another clip fires). Fired from a controller or the web console; the scene is built on a background thread during the count-in and swapped in on the grid boundary. Empty = no grid. |

## `[playlist]`

Playlist behavior + video interleaving.

```toml
[playlist]
videos_dir = "assets/videos"
interleave_videos = false
loop = true
fade_duration_s = 0.4
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`videos_dir`**<br>*Type:* `str`<br>*Default:* `'assets/videos'` | Directory of videos to interleave between scenes. |
| **`interleave_videos`**<br>*Type:* `bool`<br>*Default:* `False` | Insert a video from videos_dir after each scene (multi-scene playlists only; ignored in single-scene mode). |
| **`songlengths_file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Path to an HVSC Songlengths.md5 file; gives waveform scenes their true duration when duration_s is unset. Left unset (the default), an unpacked HVSC under assets/sids/ (either the whole C64Music/ tree or just its contents) is auto-detected. Set to an empty string to disable auto-detection. |
| **`loop`**<br>*Type:* `bool`<br>*Default:* `True` | Loop the playlist after the last scene (`--no-loop` exits after one pass; useful for 'play one video and quit'). |
| **`fade_duration_s`**<br>*Type:* `float`<br>*Default:* `0.4` | Fade-in/out duration (seconds) at scene setup/teardown: non-black pixels rise from black on entry and sink to black on a normal scene end, across every compose-based display mode. 0 disables (hard cuts). A CTRL skip aborts an in-progress fade immediately. |

## `[preview]`

Local mirror window of the C64 display.

```toml
[preview]
enabled = false
fps = 30
scale = 3
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Open a local window mirroring the U64 display (needs a desktop session; no extra required). |
| **`fps`**<br>*Type:* `int`<br>*Default:* `30` | Preview window refresh rate. |
| **`scale`**<br>*Type:* `int`<br>*Default:* `3` | Integer pixel scale factor for the preview window. |
| **`charset_path`**<br>*Type:* `str \| None`<br>*Default:* `None` | C64 character ROM used to render char modes in the preview. Unset = resolve automatically (the dump c64cast takes off your own C64 on the first run; see `--dump-char-rom`). |

## `[recording]`

Record the rendered display to a file.

```toml
[recording]
enabled = false
path = "recording.mp4"
fps = 30
scale = 2
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Record the rendered display to a video file (cv2.VideoWriter). |
| **`path`**<br>*Type:* `str`<br>*Default:* `'recording.mp4'` | Output video file path. Does not cascade from an ensemble master: a system that leaves this alone records to 'recording-<system>.mp4' so the wall's systems don't overwrite each other. Setting it explicitly uses that path verbatim. |
| **`fps`**<br>*Type:* `int`<br>*Default:* `30` | Recording frame rate. |
| **`scale`**<br>*Type:* `int`<br>*Default:* `2` | Integer pixel scale factor for the recording. |
| **`fourcc`**<br>*Type:* `str`<br>*Default:* `'mp4v'` | FourCC codec code passed to cv2.VideoWriter. |

## `[teensyrom]`

TeensyROM+ backend connection.

```toml
[teensyrom]
transport = "serial"  # serial | tcp
baud = 2000000
tcp_port = 2112
storage = "sd"        # sd | usb
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`transport`**<br>*Type:* `str`<br>*Default:* `'serial'` | TR control link: USB serial or raw TCP (port 2112). Choices: `serial`, `tcp`. |
| **`serial_port`**<br>*Type:* `str \| None`<br>*Default:* `None` | Serial device for transport=serial over a plain USB data cable (e.g. /dev/cu.usbmodem* or COM3; NOT an FTDI null-modem cable). On macOS, leave unset to auto-detect the TeensyROM by its USB serial number; required (no auto-detect yet) on other platforms. |
| **`baud`**<br>*Type:* `int`<br>*Default:* `2000000` | Serial baud rate (TR uses full USB bandwidth; 2 Mbaud 8N1). |
| **`host`**<br>*Type:* `str \| None`<br>*Default:* `None` | TR IP address for transport=tcp (find via CCGMS "ATC" or RTC sync). Required for tcp. |
| **`tcp_port`**<br>*Type:* `int`<br>*Default:* `2112` | TR TCP listener port (firmware default 2112). |
| **`storage`**<br>*Type:* `str`<br>*Default:* `'sd'` | Where helper PRGs are uploaded + launched from. Choices: `sd`, `usb`. |

## `[ultimate64]`

Ultimate 64 target + transport.

```toml
[ultimate64]
url = "http://192.168.2.64"
system = "auto"              # auto | NTSC | PAL
sid_play_rate = "auto"       # auto | off
sid_video_mode = "off"       # off | auto
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`url`**<br>*Type:* `str`<br>*Default:* `'http://192.168.2.64'` | Base URL of the Ultimate 64 (REST + DMA host). |
| **`system`**<br>*Type:* `str`<br>*Default:* `'auto'` | Machine timing standard (affects frame rate, CPU clock, SID PLAY rate). 'auto' reads it from the Ultimate's live System Mode; on a backend that can't be asked, or under `--skip-probe`, it falls back to NTSC. Choices: `auto`, `NTSC`, `PAL`. |
| **`sid_play_rate`**<br>*Type:* `str \| float`<br>*Default:* `'auto'` | PLAY-call rate for vsync-timed SID tunes. 'auto' = the tune's native frame rate from its PSID clock flag (PAL tunes at ~50.12 Hz); 'off' = leave the kernal jiffy rate alone (~60 Hz on both standards, so PAL tunes run ~20% fast — the pre-1.9 behaviour); a number pins every vsync tune to that rate in Hz. CIA-timed (multispeed) tunes always self-time and are never overridden. Choices: `auto`, `off`. |
| **`sid_video_mode`**<br>*Type:* `str`<br>*Default:* `'off'` | Switch the Ultimate's System Mode so the machine's PAL/NTSC timing matches [ultimate64].system, correcting SID pitch (phi2 differs 3.8% between standards). 'off' leaves it alone. Ultimate 64 only; live and volatile, restored at teardown. Changes the HDMI output mode. Choices: `off`, `auto`. |
| **`hdmi_scan_resolution`**<br>*Type:* `str`<br>*Default:* `'auto'` | The Ultimate 64's HDMI upscaler. 'auto' raises SD to HD (720p) only when sid_video_mode retimes the machine — PAL timing at SD puts 576p50 on the wire and some capture devices cannot lock to it, while the same machine at 720p50 captures cleanly. 'keep' never touches it; a scan-mode label sets it for the run (the 'PC' modes are passed through from the firmware but are untested under PAL timing). Live and volatile, restored at teardown. Newer U64 boards only (older firmware has no such setting). Choices: `auto`, `keep`, `SD (480p/576p)`, `HD (720p)`, `FullHD (1080p)`, `PC 800 x 600`, `PC 1024 x 768`, `PC 1280 x 1024`. |
| **`dma_port`**<br>*Type:* `int`<br>*Default:* `64` | TCP port of the U64 Ultimate DMA Service (firmware default 64). |
| **`dma_password`**<br>*Type:* `str \| None`<br>*Default:* `None` | U64 network password, if set. Prefer the C64CAST_DMA_PASSWORD env var over committing it here. |
| **`auto_reu`**<br>*Type:* `bool`<br>*Default:* `True` | Auto-enable + size the U64 REU (live, volatile, restored at teardown) for runs that hard-require it ([audio].use_reu_pump or explicit [video].use_reu_staged = true). Removes the manual F2 enable step. false = manage the REU yourself. No effect on no-REU backends or under `--skip-probe`. |
| **`sid_model`**<br>*Type:* `str`<br>*Default:* `'auto'` | Auto-configure the SID chip model (6581/8580) to match what a .sid file's PSID header requests: on the U64 by remapping to a matching physical socket or an UltiSID core, on the Ultimate II+ by setting each emulated SID that snoops a tune chip to that model. 'off' disables. An explicit '6581'/'8580' forces that model for every chip, ignoring the header. Choices: `auto`, `6581`, `8580`, `off`. |
| **`sid_panning`**<br>*Type:* `list[int \| str]`<br>*Default:* `[]` | Stereo pan per SID audio source (U64, or the Ultimate II+'s 2 emulated stereo SIDs). Max 4 entries — one pan control per source (the U64 has 2 SID sockets + 2 UltiSID cores), and entry N pans the Nth source the tune uses. Each entry is an int -5..5 (negative = left, 0 = center) or a label ('Left 3', 'Center', 'Right 2'). Empty = auto spread: 1 source centered, 2 [-3, 3], 3 [0, -3, 3], 4 [-2, 2, -5, 5] — ordered so the primary chip stays nearest center. Fewer positions exist without socketed SIDs: with none, only the 2 FPGA sources are pannable, so chips beyond the 2nd share a pan. |
| **`sid_volume`**<br>*Type:* `list[int \| str]`<br>*Default:* `[]` | Mixer level per SID audio source (U64, or the Ultimate II+'s 2 emulated stereo SIDs). Max 4 entries — one volume control per source, and entry N sets the Nth source the tune uses, same indexing as sid_panning. Each entry is a dB int (0, -6, 3) or a label ('0 dB', '-6 dB', 'off'). Empty = auto: a source the tune plays on is raised to 0 dB when it would otherwise be OFF (silent), a source already at a deliberate level is left alone, and every source the tune does not use is muted. The ladder is sparse below -18 dB: -42, -36, -30, -27, -24, then every dB from -18 to +6. |

## `[video]`

Webcam input + experimental video paths.

```toml
[video]
device = -1
use_reu_staged = "auto"
double_buffer = "auto"
setup_progress_bar = true
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`device`**<br>*Type:* `int \| str`<br>*Default:* `-1` | Webcam device: an integer cv2 index (-1 = system default camera, cv2 index 0), or a string matched to a camera by name substring (e.g. "Cam Link") or USB VID:PID (e.g. "0fd9:0066"). String selection needs the 'camera' extra; run `--list-devices` to see names + VID:PID. |
| **`use_reu_staged`**<br>*Type:* `bool \| str`<br>*Default:* `'auto'` | REU bank-swap double-buffer for video push. "auto" (default) stages bitmap modes (hires/mhires) when the startup probe finds the U64's REU enabled, leaving char modes on the cheaper host-DMA path; true forces it on for every mode, false off. auto silently falls back to host-DMA when REU isn't confirmed. |
| **`double_buffer`**<br>*Type:* `bool \| str`<br>*Default:* `'auto'` | Host-DMA double-buffer (page flip) for tear-free bitmap video where REU staging can't help. "auto" (default) enables it for bitmap modes (hires/mhires) when REU staging is off and either the backend has no REU (e.g. TeensyROM) or the scene has a text overlay (whose presence turns the REU path off to dodge bank-swap shimmer, otherwise leaving single-buffer host-DMA that tears on cuts). true forces it on for bitmap modes, false off; gated off when the REU mic pump is active (shared $0314). Independent of [video].use_reu_staged (the REU path). |
| **`setup_progress_bar`**<br>*Type:* `bool`<br>*Default:* `True` | Diagonal-striped bar along screen row 22 while a video scene buffers (container open, color pre-scan, audio encode, REU upload). No text or numbers — the right edge is 100%. The first video frame wipes it. Set false for an untouched screen during setup. |

## `[vision]`

Webcam hand-gesture control (extra).

```toml
[vision]
enabled = false
model_path = "assets/models/hand_landmarker.task"
num_hands = 1
min_detection_confidence = 0.7
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Enable webcam hand-gesture control (pinch=pause/resume, swipe=skip, open-hand=cycle). Needs the 'vision' extra. |
| **`model_path`**<br>*Type:* `str`<br>*Default:* `'assets/models/hand_landmarker.task'` | Path to the MediaPipe HandLandmarker .task model bundle (download separately; see assets/models/README.md). |
| **`num_hands`**<br>*Type:* `int`<br>*Default:* `1` | Max hands the tracker detects per frame. |
| **`min_detection_confidence`**<br>*Type:* `float`<br>*Default:* `0.7` | Minimum confidence to detect a hand (0..1). Raise it if your torso/face occasionally register as a phantom hand. |
| **`min_tracking_confidence`**<br>*Type:* `float`<br>*Default:* `0.5` | Minimum confidence to keep tracking a hand across frames (0..1). |
| **`poll_interval_s`**<br>*Type:* `float`<br>*Default:* `0.066` | Seconds between gesture-recognition ticks (~0.066 = 15 Hz). |
| **`pinch_threshold`**<br>*Type:* `float`<br>*Default:* `0.05` | Thumb-index normalized distance below which a pinch registers. |
| **`swipe_velocity`**<br>*Type:* `float`<br>*Default:* `0.4` | Wrist horizontal speed (frame-widths/sec) that triggers a skip. HW-tuned: deliberate swipes peak ~0.5-1.1, drift stays < ~0.2. |
| **`gesture_cooldown_s`**<br>*Type:* `float`<br>*Default:* `1.0` | Minimum seconds between fired gesture events (debounce). |
| **`gesture_dwell_s`**<br>*Type:* `float`<br>*Default:* `0.4` | Seconds a pose (pinch / open hand) must be held STILL before it fires (0 = first frame). With the stillness gate this rejects busy/moving hands and poses passing through on the way to a swipe. Swipe (motion) ignores it. |
| **`hold_threshold_s`**<br>*Type:* `float`<br>*Default:* `3.0` | Seconds a pinch must be held while paused to resume. |
| **`mirror`**<br>*Type:* `bool`<br>*Default:* `True` | Mirror the frame before tracking so swipe direction matches the mirrored webcam view. |
| **`performance`**<br>*Type:* `bool`<br>*Default:* `False` | Live DJ/VJ Phase 6: remap the RUNNING-state gestures to clip-launch performance actions instead of transport — swipe = launch the next [[performance.clips]] slot, pinch-hold = bypass effect layer 0, open-hand-hold = bypass effect layer 1. Off (default) keeps the transport mapping (swipe=skip, pinch=pause, open-hand=cycle style). Pinch-hold-to-resume while paused is unchanged either way. Needs a [[performance.clips]] grid for the clip-advance gesture to do anything. |

## `[web]`

Web console host (`--serve`): a long-lived server that owns the hardware and starts/stops sessions on request (extra).

```toml
[web]
enabled = false
host = "127.0.0.1"
port = 8123
autostart = false
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`enabled`**<br>*Type:* `bool`<br>*Default:* `False` | Run the web console host instead of a one-shot session — the server owns the C64 and starts/stops shows on request (same as `--serve`); requires the 'web' extra. |
| **`host`**<br>*Type:* `str`<br>*Default:* `'127.0.0.1'` | Bind address for the web console host. |
| **`port`**<br>*Type:* `int`<br>*Default:* `8123` | Bind port for the web console host. |
| **`token`**<br>*Type:* `str`<br>*Default:* `''` | Shared token required on every web-console request. Empty = read `token_file`, else generate one under the data dir and print it at startup (this surface is never unauthenticated). Prefer the C64CAST_WEB_TOKEN env var. |
| **`token_file`**<br>*Type:* `str`<br>*Default:* `''` | Read the shared token from this file instead of storing it in the config (one line, whitespace-stripped). Ignored when `token` or C64CAST_WEB_TOKEN is set. |
| **`viewer_token`**<br>*Type:* `str`<br>*Default:* `''` | Optional second token granting read-only access (GET/HEAD only): watch the state feed, but never start, stop or edit. Prefer the C64CAST_WEB_VIEWER_TOKEN env var. |
| **`autostart`**<br>*Type:* `bool`<br>*Default:* `False` | Start the config the host was launched with as soon as it comes up, rather than waiting for a browser to ask (headless / launchd boxes). |
| **`settle_s`**<br>*Type:* `float`<br>*Default:* `3.0` | Seconds to leave the hardware alone between tearing one session down and building the next: the U64's DMA service refuses new connections for a few seconds after one closes, and a camera will not reopen instantly. |
| **`config_roots`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Directories the web console may browse and edit .toml configs in. Empty = the directory the host was launched from. Nothing outside these is readable or writable, symlinks included; a config saved here can still name media anywhere, so treat write access as shell-equivalent. |

## `[wled]`

Two-directional WLED bridge: broadcast SID audio-sync out (Mode 3) and/or act as a virtual WLED device the app can control (Mode 1).

```toml
[wled]
rate_hz = 50
broadcast_tempo_fallback = false
name = "c64cast"
```

<!-- table: fields -->
| Field | Description |
|---|---|
| **`broadcast`**<br>*Type:* `str \| None`<br>*Default:* `None` | Mode 3 (audio-sync out). 'disabled' (default) \| 'enabled' \| '[host][:port]'. 'enabled' multicasts to WLED's default group 239.0.0.1:11988 (every WLED with 'Receive' enabled reacts); give a unicast '[host][:port]' to target one device. |
| **`rate_hz`**<br>*Type:* `float`<br>*Default:* `50.0` | Broadcast rate in Hz (Mode 3). WLED expects roughly frame-rate updates; ~40-60 is typical. |
| **`broadcast_tempo_fallback`**<br>*Type:* `bool`<br>*Default:* `False` | Mode 3 performance glue (Live DJ/VJ Phase 6): when the on-screen scene has NO SID features to broadcast (a video, webcam, or slideshow), fall back to the [performance] beat grid so WLED strips keep pulsing to the MIDI/tap tempo instead of going dark. The synthesized pulse spikes on each beat (from the TempoClock phase); only active while the grid is running. Off (default) = a non-SID scene broadcasts nothing, matching pre-Phase-6 behavior. A SID-driven scene always wins over the fallback. |
| **`listen`**<br>*Type:* `str \| None`<br>*Default:* `None` | Mode 1 (control surface in). 'disabled' (default) \| 'enabled' \| '[host][:port]'. 'enabled' binds the WLED JSON API on 0.0.0.0:8080; override the bind with '[host][:port]'. Needs the 'wled' extra. |
| **`name`**<br>*Type:* `str`<br>*Default:* `'c64cast'` | Friendly/mDNS device name advertised in Mode 1 (what the WLED app shows for this virtual device). |
