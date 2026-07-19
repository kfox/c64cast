# scripts/diags — reusable diagnostic / investigation tools

Committed home for the throwaway-but-recurring tools used during feature work
and hardware debugging. Before this directory existed they were re-created each
session in `/tmp` (with the usual `/tmp` vs `/private/tmp` and project-home
papercuts) and each invocation needed a fresh permission approval. Committing
them gives stable paths, a single permission allowlist entry, and a place to
improve them over time.

These are **dev tools, not part of the shipped package** — they live under
`scripts/` alongside `bench.py` / `fake_u64.py`, are not imported by
`c64cast/`, and are excluded from the wheel and from `mypy --strict`.

## Conventions

- **Shared helper:** [`_diaglib.py`](_diaglib.py) solves path/import handling,
  hardware defaults, and the U64 REST shims once. Every tool does
  `import _diaglib as d` and runs from anywhere (it inserts the repo root onto
  `sys.path`, so `import c64cast` works regardless of cwd).
- **Run them** with the project interpreter so `import c64cast` resolves:
  `uv run scripts/diags/<tool>.py …`, or just `scripts/diags/<tool>.py …`
  when direnv has activated `.venv`.
- **Outputs** (captures, fixtures) land in `scripts/diags/out/`, which is
  git-ignored. Source tools are tracked; their artifacts are not.
- **Hardware defaults are env-overridable** because indices/IPs drift with
  hotplug + DHCP. The committed defaults are confirmed-working values, not
  ground truth — local specifics live in auto-memory:
  | Var | Default | Meaning |
  |-----|---------|---------|
  | `C64_DIAG_URL` | `http://192.168.2.64` | U64 base URL |
  | `C64_DIAG_U2P_URL` | `http://192.168.2.65` | U2+ base URL |
  | `C64_DIAG_CV2` | `0` | Cam Link cv2 capture index |
  | `C64_DIAG_AVF_AUDIO` | `:3` | Cam Link avfoundation audio device |
  | `C64_DIAG_VERIFY_WIDTH` | `960` | longest-edge px for captures saved via `save_image` (downscale default) |

## Tools

| Tool | What it does |
|------|--------------|
| [`u64_probe.py`](u64_probe.py) | REST reachability + DMA-service (port 64) check; `--reset` / `--reset-only`. |
| [`hdmi_capture.py`](hdmi_capture.py) | Grab still frame(s) from the Cam Link (VIC ground-truth) → `out/`. Downscales to `--width` (default 960px) so captures read back cheaply; `--full` keeps native 1080p for pixel-peeking. New capture tools should write via `_diaglib.save_image` for the same default. |
| [`audio_capture.py`](audio_capture.py) | Record Cam Link audio via ffmpeg/avfoundation + `volumedetect` level summary. |
| [`run_and_capture.py`](run_and_capture.py) | Launch c64cast with a config, capture A/V across the run, then stop + reset. |
| [`make_fixtures.py`](make_fixtures.py) | Generate synthetic tone/clip/test-pattern A/V fixtures for the video path. |
| [`video_render_probe.py`](video_render_probe.py) | Render a video through a display mode offline (no HW); reports per-frame bg0/$D021 flips + bitmap full-upload churn for flash/flicker diagnosis. |
| [`doublebuffer_tear_ab.py`](doublebuffer_tear_ab.py) | A/B single-buffer vs host-DMA double-buffer for scene-cut tearing on a bitmap + text-overlay scene. Builds an abrupt-cut test video, runs both paths on the U64, burst-grabs Cam Link frames, classifies top/bottom raster-split tears, and saves example frames. Resets on exit. |
| [`dsp_ab.py`](dsp_ab.py) | Offline A/B of the host audio DSP chain on the 4-bit DAC stream (no HW): legacy vs `[dsp]` encode, objective metrics (RMS/crest/codes/loud-body DR/silence%) + reconstructed wavs to `out/`. Tune DSP params before spending a hardware capture. |
| [`dsp_noise.py`](dsp_noise.py) | Noise-stage A/B (no HW): legacy mic hard gate vs the DSP expander on the Kaggle speech-noise-dataset's matched clean↔noisy pairs. Reports gap residual, gate chatter (events/s), and speech retention; writes both reconstructed wavs to `out/`. |
| [`tr_read_probe.py`](tr_read_probe.py) | TeensyROM+ ReadC64Mem (0x64FD) round-trip over `--tcp`/`--serial`: ROM read, RAM write/read compare, live `$028D` watch. No Cam Link needed. |
| [`tr_dma_cycleclean.py`](tr_dma_cycleclean.py) | Confirm the TR+ WriteC64Mem DMA is cycle-clean: hammer `$4000` while a fragile IRQ-driven BASIC border-cycler runs; the border keeps sweeping (alive) iff the running program survived. |
| [`tr_audio_sid_probe.py`](tr_audio_sid_probe.py) | Drive the TR backend's audio paths on HW + capture Cam Link audio (`volumedetect`): `--mode tone` (host-DMA NMI DAC) or `--mode sid` (run_sid_player). `--flash` adds a 1 Hz `$D020` A/V sync marker. Silences + resets on exit. |
| [`midi_drive.py`](midi_drive.py) | Drive c64cast's `[midi_control]` surface from a **virtual MIDI port** (no physical controller): sends notes/CCs/PC/MMC-sysex from a script (`--script`), one-shot (`--send`), or interactively (`-i`). The reusable form of the `midi_smoke.py` throwaways used to HW-verify MidiScene + MIDI live-tune (transport / audio resync). Open the port before booting c64cast (point its `[midi_control].port` at it). |

## End-of-session rule

Anything that drives the machine should leave it clean: `run_and_capture.py`
resets on exit by default (`--no-reset` to keep state), and
`u64_probe.py --reset-only` is the manual hook. See the
`silence-and-reset-after-testing` note in auto-memory.
