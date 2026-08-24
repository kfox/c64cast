---
number: G
generated: true
---

# Command-Line Flags

Every option `c64cast` accepts, in the groups `-h` prints them in. A flag given here beats the same setting in a configuration file, which beats machine settings, which beats the built-in default.

## Positional Arguments

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`MEDIA`** | Quick-playback media: files, directories, globs, or URLs played in order, once (no loop unless `--loop`). Each maps to a scene by kind: video->video, .sid->waveform, image->slideshow, .prg/.crt->launcher, URL->video. Omit to run from `--config` / ./c64cast.toml / defaults. Mutually exclusive with `--config`. |

## Options

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`-h`, `--help`** | show this help message and exit |
| **`--version`** | show program's version number and exit |
| **`--config`**<br>`CONFIG` | Path to TOML config, or example:NAME for a packaged demo (see `--list-examples`) (default: ./c64cast.toml if it exists) |

## Connection

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`-u`, `--url`**<br>`TARGET` | Connection target selecting the hardware backend + endpoint (default: $C64CAST_URL, else http://192.168.2.64). Schemes: u64://HOST or http(s)://HOST (Ultimate 64 / II+); tr:// (TeensyROM+ USB serial, auto-detected), tr:///dev/cu.usbmodemXYZ or tr://COM3 (serial device), tr://HOST (TeensyROM+ TCP). Rare knobs as query params, e.g. u64://host?dma_port=64 or tr://host?tcp_port=2113. |
| **`-s`, `--system`**<br>`NTSC`, `PAL` | Target system timing (default: auto) |
| **`--sid-model`**<br>`auto`, `6581`, `8580`, `off` | Auto-configure the SID chip model per .sid PSID header, remapping to a matching physical socket or an UltiSID core if needed ('off' disables) (default: auto) |

## Quick Playback (With Media Args)

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--display`**<br>`DISPLAY` | VIC-II display mode for quick-playback video/slideshow scenes (default: mhires). |
| **`-t`, `--duration`**<br>`DURATION` | Seconds for quick-playback scenes that honor it (waveform/slideshow). |

## Video Input

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`-d`, `--device`**<br>`INDEX\|NAME\|VID:PID` | Webcam device: int index (-1 = system default), or a camera name substring / USB VID:PID (e.g. "Cam Link", "0fd9:0066"; needs the 'camera' extra) (default: -1) |

## Audio

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--audio`, `--no-audio`** | Stream audio to the 4-bit SID volume DAC; `--no-audio` mutes (default: True) |
| **`-D`, `--audio-device`**<br>`AUDIO_DEVICE` | Audio input device: an int index (-1 = system default microphone), or a device name substring (needs the 'mic' extra) (default: -1) |
| **`-r`, `--sample-rate`**<br>`SAMPLE_RATE` | Audio sample rate in Hz (default: 12000) |
| **`-m`, `--mic-sensitivity`**<br>`MIC_SENSITIVITY` | Microphone input gain multiplier (default: 1.5) |
| **`-n`, `--noise-gate`**<br>`NOISE_GATE` | Threshold below which mic input is muted (default: 0.05) |
| **`--dac-calibration-profile`**<br>`NAME\|PATH` | Override the auto-derived DAC calibration file key, for both `--calibrate-dac` and playback. A name keys a file under calibration/dac/profile-<name>.json, or names an existing file there as-is, e.g. the device-keyed 'ultimate-<id>' files `--calibrate-dac` writes (use when a TeensyROM+ moves between physical C64s: name each host's calibration once, reuse the name on every run there); a path (ending .json, or containing a separator) names a calibration file directly, which is how one machine's calibration is reused from another backend (default: None) |

## Vision Input

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--vision`** | Enable webcam hand-gesture control (pinch=pause/resume, swipe=skip, open-hand=cycle); needs the 'vision' extra (default: False) |
| **`--vision-model`**<br>`VISION_MODEL` | Path to the MediaPipe HandLandmarker .task model (default: assets/models/hand_landmarker.task) |

## Playlist

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--videos`**<br>`VIDEOS` | Directory containing videos (.mp4, .avi, .mkv, .mov, .webm, .m4v) (default: assets/videos) |
| **`--loop`, `--no-loop`** | Loop the playlist after the last scene finishes (`--no-loop` = exit after one pass; useful for "play one video and quit") (default: True) |

## Web Console

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--serve`** | Run the web console host instead of a one-shot session: an HTTP server that owns the hardware and starts/stops shows on request (default bind 127.0.0.1:8123; configure under [web]; requires the 'web' extra). Prints a login URL carrying the shared token. |

## Introspection

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--list-scenes`** | List scene types and exit |
| **`--list-overlays`** | List overlays and exit |
| **`--list-modes`** | List display modes and exit |
| **`--describe`**<br>`NAME` | Describe a scene/overlay/section/mode and exit. Prefix to disambiguate: scene:, overlay:, section:, mode: (e.g. `--describe` overlay:clock) |
| **`--compat`** | Print the overlay × display-mode compatibility matrix and exit |
| **`--list-examples`** | List the example configs that ship with c64cast (run one with `--config example:NAME`) and exit |
| **`--print-example`**<br>`NAME` | Print a packaged example config to stdout and exit — redirect it to a file to make it yours (`--print-example hello > c64cast.toml`) |
| **`--print-schema`** | Print the JSON Schema for the TOML config and exit (point your editor's `#:schema` at it for autocomplete) |
| **`--print-schema-path`** | Print where this install's JSON Schema lives — the value for a config's `#:schema` first line, worked out for `--config`'s location (default ./c64cast.toml) — and exit. Naming the installed copy is what makes the line outlive upgrades |
| **`--suggest-palette`**<br>`FILE` | Analyze an image or video and print the C64 colors that best represent it (ranked, faithful subset) for [color].force_palette_colors, then exit. No hardware. |
| **`--init`**<br>`PATH` | Interactively build a config file (needs the 'wizard' extra). Optional PATH sets the output file (default ./c64cast.toml) |
| **`--midi-setup`** | MIDI-learn wizard: press/twist your controller's buttons and knobs, then save a reusable controller profile (needs the 'midi' + 'wizard' extras). A plain run then picks it up via [midi_control].controller_profile = 'auto'. No hardware target needed. |
| **`--save-settings`** | Persist this invocation's machine-relevant flags (`-u/--url`, `-d/--device`, `--sid-model`, `--system`) into the machine-settings file ($C64CAST_SETTINGS, else ~/.config/c64cast/settings.toml), then exit. Merges with any existing file; secrets are never written. |
| **`--dump-char-rom`** | Read the character ROM out of the C64 you're connected to and cache it, then exit. C64 text then renders in the real C64 font instead of a built-in ASCII substitute. This normally happens by itself on the first run; use the flag to re-dump (e.g. after swapping in a different character ROM). |
| **`--install-char-rom`**<br>`PATH` | Install an existing character ROM dump (2 KB or 4 KB) from PATH instead of reading one off the C64, then exit. For machines c64cast can't dump from. No hardware needed. |

## Updates

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`--check-for-updates`** | Query PyPI for the latest c64cast release and report whether it's newer than this install, then exit. No config, no hardware, no mutation — see `--upgrade` to act on the answer. |
| **`--write-state`** | With `--check-for-updates`, also record the answer at <data root>/update_check.json, for the web console's update banner and (on the appliance image) the login MOTD to read without querying PyPI themselves. No effect without `--check-for-updates`. |
| **`--motd-line`** | Print the pending-upgrade line from the last `--write-state` check (or nothing, if none is pending), then exit. Never queries PyPI — for an appliance's /etc/update-motd.d/ script. |
| **`--upgrade`** | Detect how this install was made (uv tool, pipx, pip, or a development checkout) and run that installer's own upgrade command, which preserves whichever extras are already installed. Prompts for confirmation unless `--yes`. |
| **`--yes`** | Skip `--upgrade`'s confirmation prompt (for scripts/CI). No effect without `--upgrade`. |
| **`--reset-setup`** | Clear the appliance's first-run setup marker, then exit — the next `--serve` with [web].setup_wizard on will ask again rather than opening the normal token-gated console. No effect on a config with setup_wizard off. |

## Debug

<!-- table: fields -->
| Flag | Description |
|---|---|
| **`-v`, `--verbose`** | Increase log verbosity (default: INFO; -v enables DEBUG) |
| **`--heartbeat`**<br>`HEARTBEAT` | Health heartbeat interval in seconds, 0 disables (default: 10.0) |
| **`--skip-probe`** | Skip the startup U64 reachability probe (default: False) |
| **`--list-devices`** | List available audio and video input devices and exit |
| **`--doctor`** | Validate the whole config (all scenes/overlays at once), check optional extras + probe each U64, then exit. Add `--skip-probe` for a fast, offline, hardware-free config check. |
| **`--calibrate-dac`** | Measure the connected SID's Mahoney 8-bit $D418 DAC transfer curve (requires a capture device — Cam Link — on the SID audio output) and save a per-device calibrated table, then exit. On a U64/U2+, every populated physical SID socket is measured independently. Playback with [audio].dac_curve = 'auto' (the default) then uses the applicable table automatically. Most valuable for physical 6581/8580 chips and SID replacements, which vary chip-to-chip. |
| **`--log-file`**<br>`PATH` | Mirror log output to PATH (useful for headless runs) |
| **`--profile`, `--no-profile`** | Emit per-scene frame timing summaries (cpu_render / compose / push / wait, plus DMA writes/bytes per frame) (default: False) |
| **`--profile-interval`**<br>`SECONDS` | Seconds between profiler summary lines (default: 10.0) |
| **`--frame-numbers`** | Overlay playback timecode + source frame number on video frames (debug aid for locating flashing frames) (default: False) |
| **`--overwrite`** | On exit, silently save any live-tune parameter changes (made via MIDI/WLED during the run) back into the config's [color] section (keeping a .bak), instead of prompting. No effect if nothing changed or the run has no config file. |
