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
  hotplug + DHCP. The committed defaults are a working rig's values, not ground
  truth; set the vars for yours:
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
| [`video_render_probe.py`](video_render_probe.py) | Render a video through a display mode offline (no HW); reports per-frame bg0/$D021 flips, bitmap push churn, and **writes + modelled frame cost per region** (priced with the profile's measured link cost model) for flash/flicker and throughput diagnosis. Flags any region `write_region` split into more writes than pushing it whole would have cost. Also times **host CPU per frame** (decode / render) against that link cost and names which side, if either, binds the source frame rate — with `--threads 1` to compare machines by single-core speed, this is the candidate-board benchmark. |
| [`link_cost_model.py`](link_cost_model.py) | Measure what a write costs the **host link** in wall-clock time, as `max(floor, intercept + per_byte × B)`, and print what it implies for `write_region`'s chunking. Times bursts flush-to-flush and separates the fixed from the marginal cost with a two-stage fit. This is where `HardwareProfile.write_cost_*` comes from — re-run it per backend if firmware or transport changes. Resets on exit. |
| [`doublebuffer_tear_ab.py`](doublebuffer_tear_ab.py) | A/B single-buffer vs host-DMA double-buffer for scene-cut tearing on a bitmap + text-overlay scene. Builds an abrupt-cut test video, runs both paths on the U64, burst-grabs Cam Link frames, classifies top/bottom raster-split tears, and saves example frames. Resets on exit. |
| [`flicker_tear_ab.py`](flicker_tear_ab.py) | Measure how often, and *where*, the host-DMA `$DD00` bank swap lands inside the visible picture. Acceptance test for the `$D012` window gate in `modes_irq`'s two swap handlers: builds an 8-state test video whose period cannot alias with the 2-bank swap, runs flicker and plain hires, classifies each captured frame's rows against the known palette, and reports percent torn plus the seam position — the seam is the payload, since the halt hypothesis predicts where the swap lands, not merely that it is late. Read the phase logs' fps too: a fix that buys cleanliness with frame rate is not a fix. Resets on exit. |
| [`dsp_ab.py`](dsp_ab.py) | Offline A/B of the host audio DSP chain on the 4-bit DAC stream (no HW): legacy vs `[dsp]` encode, objective metrics (RMS/crest/codes/loud-body DR/silence%) + reconstructed wavs to `out/`. Tune DSP params before spending a hardware capture. |
| [`dsp_noise.py`](dsp_noise.py) | Noise-stage A/B (no HW): legacy mic hard gate vs the DSP expander on the Kaggle speech-noise-dataset's matched clean↔noisy pairs. Reports gap residual, gate chatter (events/s), and speech retention; writes both reconstructed wavs to `out/`. |
| [`tr_read_probe.py`](tr_read_probe.py) | TeensyROM+ ReadC64Mem (0x64FD) round-trip over `--tcp`/`--serial`: ROM read, RAM write/read compare, live `$028D` watch. No Cam Link needed. |
| [`tr_dma_cycleclean.py`](tr_dma_cycleclean.py) | Confirm the TR+ WriteC64Mem DMA is cycle-clean: hammer `$4000` while a fragile IRQ-driven BASIC border-cycler runs; the border keeps sweeping (alive) iff the running program survived. |
| [`tr_audio_sid_probe.py`](tr_audio_sid_probe.py) | Drive the TR backend's audio paths on HW + capture Cam Link audio (`volumedetect`): `--mode tone` (host-DMA NMI DAC) or `--mode sid` (run_sid_player). `--flash` adds a 1 Hz `$D020` A/V sync marker. Silences + resets on exit. |
| [`midi_drive.py`](midi_drive.py) | Drive c64cast's `[midi_control]` surface from a **virtual MIDI port** (no physical controller): sends notes/CCs/PC/MMC-sysex from a script (`--script`), one-shot (`--send`), or interactively (`-i`). The reusable form of the `midi_smoke.py` throwaways used to HW-verify MidiScene + MIDI live-tune (transport / audio resync). Open the port before booting c64cast (point its `[midi_control].port` at it). |
| [`mahoney_slot_ring_probe.py`](mahoney_slot_ring_probe.py) | Drive the `--calibrate-dac` slot-ring primitive against one isolated SID source (`--source socket1\|socket2\|ultisid1\|ultisid2`) and **save the raw captures to `.npy`**, so the alignment/extraction can be re-run offline with `--replay` and no hardware. Prints per-ring diagnostics, the merged 256-code ladder, and the volume-0 self-test. `--rounds N` sweeps how many slot-order rotations get averaged. `--source ultisid1` is the only way to measure an emulated core alone at `$D400` — `--calibrate-dac` measures physical sockets only — and is how the shipped `MAHONEY_ULTISID` table is re-derived. Restores the SID config and resets on exit. |
| [`dac_curve_playback_ab.py`](dac_curve_playback_ab.py) | Objective A/B of the `$D418` DAC curves: plays one test tone through the real encoder per curve (`linear` / `mahoney_ultisid` / the applicable calibrated table), captures it, and reports SNDR + THD at full scale and −30 dBFS. Use this rather than listening — the curves differ by several dB in loudness, which a level-mismatched ear reads as "better". |
| [`midi_monitor.py`](midi_monitor.py) | The read counterpart to `midi_drive.py`: **print what a physical controller sends** and the exact value to paste into a config (`pad = N`, `type = "cc", number = N`). Lists/selects the input port (substring `--port`, or a numbered prompt), streams each note/CC/PC/pitchbend, and on Ctrl+C prints a summary table that separates a fixed-note pad from a full-sweep knob/fader. Hides MIDI clock unless `--clock`. Reuses `classify_message`, so it reads a controller identically to the live listener. Needs the `midi` extra. |

## End-of-session rule

Anything that drives the machine should leave it clean: `run_and_capture.py`
resets on exit by default (`--no-reset` to keep state), and
`u64_probe.py --reset-only` is the manual hook. Silence the SID (`$D418` = 0)
and disable CIA #2 NMIs before you reset, or the next run inherits a screaming
chip and a live NMI source.
