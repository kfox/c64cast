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

## Tools

| Tool | What it does |
|------|--------------|
| [`u64_probe.py`](u64_probe.py) | REST reachability + DMA-service (port 64) check; `--reset` / `--reset-only`. |
| [`hdmi_capture.py`](hdmi_capture.py) | Grab still frame(s) from the Cam Link (VIC ground-truth) → `out/`. |
| [`audio_capture.py`](audio_capture.py) | Record Cam Link audio via ffmpeg/avfoundation + `volumedetect` level summary. |
| [`run_and_capture.py`](run_and_capture.py) | Launch c64cast with a config, capture A/V across the run, then stop + reset. |
| [`make_fixtures.py`](make_fixtures.py) | Generate synthetic tone/clip/test-pattern A/V fixtures for the video path. |
| [`video_render_probe.py`](video_render_probe.py) | Render a video through a display mode offline (no HW); reports per-frame bg0/$D021 flips + bitmap full-upload churn for flash/flicker diagnosis. |
| [`dsp_ab.py`](dsp_ab.py) | Offline A/B of the host audio DSP chain on the 4-bit DAC stream (no HW): legacy vs `[dsp]` encode, objective metrics (RMS/crest/codes/loud-body DR/silence%) + reconstructed wavs to `out/`. Tune DSP params before spending a hardware capture. |
| [`dsp_noise.py`](dsp_noise.py) | Noise-stage A/B (no HW): legacy mic hard gate vs the DSP expander on the Kaggle speech-noise-dataset's matched clean↔noisy pairs. Reports gap residual, gate chatter (events/s), and speech retention; writes both reconstructed wavs to `out/`. |

## End-of-session rule

Anything that drives the machine should leave it clean: `run_and_capture.py`
resets on exit by default (`--no-reset` to keep state), and
`u64_probe.py --reset-only` is the manual hook. See the
`silence-and-reset-after-testing` note in auto-memory.
