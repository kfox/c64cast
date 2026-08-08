"""Config-free and single-shot CLI commands.

Everything here is a terminal command in its own right — it prints, returns
an exit code, and never starts the playlist session: `--list-devices`, the
introspection family (`--list-*` / `--describe` / `--print-schema` /
`--suggest-palette`), `--save-settings`, `--install-char-rom`,
`--dump-char-rom`, `--calibrate-dac`, and `--doctor`. Split out of cli.py
(2026-08) so that module keeps two jobs (arg parsing + the session run)
instead of ten. Bodies moved verbatim; exit codes and log/print text are
unchanged. The logger is the same "c64cast" logger cli.py uses, so
assertLogs-style captures and user-facing output are identical.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import time

from c64cast.audio import dac_calibration
from c64cast.audio.audio import AUDIO_AVAILABLE, resolve_audio_input_device
from c64cast.audio.dac_capture_device import CaptureUnavailableError
from c64cast.audio.dac_slot_ring import MeasurementError
from c64cast.hw import char_rom
from c64cast.hw.backend import make_backend

from . import config as cfgmod
from . import paths
from ._native_io import silence_native_stderr

log = logging.getLogger("c64cast")


def configure_logging(verbosity: int, log_file: str | None = None) -> None:
    """Wire up the root logger.

    Terminal: RichHandler (color + columns) when `rich` is installed; plain
    StreamHandler otherwise. File: when `log_file` is given, also append to
    that path with a verbose plain-text format. Safe to call more than once
    — clears any existing handlers first so a re-call (e.g. after config
    load) doesn't double up."""
    # Default level is INFO so the user sees lifecycle messages (scene
    # transitions, audio bring-up, keypress detection, resets) without
    # needing -v. -v / -vv bumps to DEBUG.
    level = logging.INFO
    if verbosity >= 1:
        level = logging.DEBUG

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    try:
        # rich is an optional [logging] extra; pyright doesn't see it unless installed.
        from rich.logging import RichHandler  # pyright: ignore[reportMissingImports]

        terminal: logging.Handler = RichHandler(
            level=level,
            show_path=False,
            rich_tracebacks=True,
            log_time_format="%H:%M:%S",
        )
        terminal.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    except ImportError:
        terminal = logging.StreamHandler()
        terminal.setLevel(level)
        terminal.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        )
    root.addHandler(terminal)

    if log_file:
        try:
            fh = logging.FileHandler(paths.expand_user(log_file), encoding="utf-8")
        except OSError as e:
            # Don't let a bad --log-file path kill the run; surface and
            # continue with just the terminal handler.
            log.warning("could not open log file %s: %s", log_file, e)
        else:
            fh.setLevel(level)
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                )
            )
            root.addHandler(fh)

    # Third-party loggers that spam at DEBUG and drown our own output under -vv.
    # urllib3 logs every REST request/connection to the U64 (probe, config
    # reads, run_prg) — pin it to WARNING so -vv stays about c64cast, not the
    # HTTP transport. (Requests reuse the connection pool, so this is pure noise.)
    for noisy in ("urllib3.connectionpool", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def list_devices() -> int:
    print("Audio input devices (use with -D / --audio-device — an index or a name substring):")
    if AUDIO_AVAILABLE:
        import sounddevice as sd

        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        any_input = False
        for idx, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            any_input = True
            marker = " *" if idx == default_in else "  "
            print(
                f" {marker}[{idx}] {d['name']} "
                f"({d['max_input_channels']}ch @ {int(d['default_samplerate'])} Hz)"
            )
        if not any_input:
            print("    (no input-capable audio devices found)")
    else:
        print("    (sounddevice not installed)")

    print()
    print("Video input devices (use with -d / --device — an index, a name substring, or VID:PID):")
    import cv2

    from . import camera

    # Best-effort resolution probe (indices 0-7), merged into whichever listing
    # we print below. Probing past the highest valid index makes OpenCV (and the
    # AVFoundation / FFmpeg backends underneath it) print to stderr at the C
    # level, so mute that for the probe via fd-level redirection.
    res_by_index: dict[int, tuple[int, int]] = {}
    sys.stdout.flush()
    with silence_native_stderr():
        for idx in range(8):
            cap = cv2.VideoCapture(idx)
            try:
                if cap is not None and cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    res_by_index[idx] = (w, h)
            finally:
                if cap is not None:
                    cap.release()

    # Rich path (the `camera` extra): name + USB VID:PID + the correct backend
    # index cross-platform. This makes system_profiler's index-guessing dance
    # unnecessary, so we return before the macOS fallback below.
    cams = camera.enumerate_cameras()
    if cams:
        for c in cams:
            line = f"   [{c.index}] {c.name}"
            vp = c.vidpid_str()
            if vp:
                line += f"  ({vp})"
            res = res_by_index.get(c.index)
            if res:
                line += f"  {res[0]}x{res[1]}"
            print(line)
        return 0

    if not camera.camera_enumeration_available():
        print(
            "    (install the 'camera' extra for names + VID:PID: "
            "uv tool install --force 'c64cast[all]')"
        )
    if res_by_index:
        for idx in sorted(res_by_index):
            w, h = res_by_index[idx]
            print(f"   [{idx}] {w}x{h}")
    else:
        print("    (no webcams responded to OpenCV probe)")

    if sys.platform == "darwin":
        # Prefer the jq pipeline when jq is on PATH — it collapses
        # system_profiler's verbose multi-line dump into a clean
        # `index:name` listing that lines up with AVFoundation's (and
        # therefore OpenCV's) device enumeration. Falls back to the raw
        # dump when jq isn't installed.
        cmd = (
            [
                "sh",
                "-c",
                "system_profiler -json SPCameraDataType 2>/dev/null | "
                "jq -r '.SPCameraDataType[]._name' | nl -v0 -w1 -s:",
            ]
            if shutil.which("jq")
            else ["system_profiler", "SPCameraDataType"]
        )
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            print()
            print("macOS cameras (system_profiler SPCameraDataType):")
            for line in out.stdout.splitlines():
                if line.strip():
                    print(f"    {line.rstrip()}")
    return 0


def _collect_lab_samples(path: str):
    """Decode ``path`` (image or video/URL) into the CIE-Lab sample reservoir
    `suggest_palette` ranks over. Returns the (N, 3) float32 array, or None when
    the file can't be read. Images load via cv2; videos/URLs reuse the shared
    color pre-scan (`video.scan_video_samples`), so the same sampling that
    feeds force_palette/auto_fit feeds the suggestion."""
    import cv2

    from .palette import ColorMapAccumulator
    from .scene_factory import VIDEO_EXTS
    from .video import scan_video_samples

    acc = ColorMapAccumulator()  # accumulate only; we want its raw lab_samples()
    ext = os.path.splitext(path)[1].lower()
    is_url = path.lower().startswith(("http://", "https://"))
    if not is_url and ext not in VIDEO_EXTS:
        # Treat anything non-video (and non-URL) as an image.
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        acc.add(img)
    elif not scan_video_samples(path, [acc]):
        return None
    samples = acc.lab_samples()
    return samples if samples.size else None


def _format_suggest_palette(path: str, ranked: list[tuple[int, float]]) -> str:
    """Render the `suggest_palette` ranking as a table plus a paste-ready
    `force_palette_colors` line (top-8, a reasonable default the user can trim)."""
    from .palette import C64_COLOR_NAMES

    lines = [
        f"Best-fit C64 palette for {os.path.basename(path)} (faithful subset, ranked by value):",
        "",
        "  rank  idx  color          mean Lab err",
        "  ----  ---  -------------  ------------",
    ]
    for rank, (idx, err) in enumerate(ranked, start=1):
        lines.append(f"  {rank:>4}  {idx:>3}  {C64_COLOR_NAMES[idx]:<13}  {err:>10.1f}")
    top = [idx for idx, _ in ranked[:8]]
    lines += [
        "",
        "Mean Lab error falls as colors are added; its knee shows where extra colors stop helping.",
        "Pick a prefix for [color].force_palette_colors, e.g. top 8:",
        "",
        f"    force_palette_colors = {top}",
    ]
    return "\n".join(lines)


def run_suggest_palette(path: str) -> int:
    """`--suggest-palette FILE`: rank the C64 colors that best (faithfully)
    represent an image/video and print them for `force_palette_colors`. No
    config, no hardware."""
    from .palette import suggest_palette

    if not path.lower().startswith(("http://", "https://")) and not os.path.exists(path):
        print(f"suggest-palette: file not found: {path}", file=sys.stderr)
        return 2
    samples = _collect_lab_samples(path)
    if samples is None:
        print(
            f"suggest-palette: could not read color samples from {path} "
            "(unsupported/corrupt file, or the 'video' extra is missing for video input)",
            file=sys.stderr,
        )
        return 2
    print(_format_suggest_palette(path, suggest_palette(samples)))
    return 0


def run_introspection(args: argparse.Namespace) -> int | None:
    """Handle the config-introspection commands (--list-*, --describe,
    --compat, --print-schema, --suggest-palette). Returns an exit code when one
    fired, else None so main() continues to the normal run path. These need no
    config file or hardware."""
    from . import introspect

    if args.list_scenes:
        print(introspect.render_list_scenes())
        return 0
    if args.list_overlays:
        print(introspect.render_list_overlays())
        return 0
    if args.list_modes:
        print(introspect.render_list_modes())
        return 0
    if args.compat:
        print(introspect.render_compat())
        return 0
    if args.list_examples:
        print(introspect.render_list_examples())
        return 0
    if args.print_example is not None:
        # Straight to stdout so `> c64cast.toml` makes an editable copy — the
        # packaged original lives inside the install and isn't meant to be
        # edited in place. A bad name must NOT exit 0: the caller is usually
        # redirecting, and a happy exit would leave them an empty config.
        try:
            text = paths.resolve_example(args.print_example).read_text(encoding="utf-8")
        except ValueError as e:
            configure_logging(args.verbose or 0, args.log_file)
            log.error("%s", e)
            return 2
        print(text, end="")
        return 0
    if args.describe is not None:
        print(introspect.render_describe(args.describe))
        return 0
    if args.print_schema:
        import json

        from . import schema

        print(json.dumps(schema.build_schema(), indent=2))
        return 0
    if args.suggest_palette is not None:
        return run_suggest_palette(args.suggest_palette)
    if getattr(args, "midi_setup", False):
        from . import midi_setup

        return midi_setup.run_setup()
    if args.init is not None:
        from . import wizard

        result = wizard.run_init(args.init or None)
        if result is None:
            return 2  # canceled, or the 'wizard' extra is missing
        out_path, launch = result
        if launch:
            # Fall through to the normal run path against the file we just
            # wrote (returning None lets main() continue to load_master).
            args.config = out_path
            return None
        return 0
    return None


def run_save_settings(args: argparse.Namespace) -> int:
    """Persist this invocation's machine-relevant flags into the machine-
    settings file, then exit.

    Savable whitelist (v1): the ``-u/--url`` connection target (decomposed via
    :func:`connect.parse_connection_uri` exactly as the run path does),
    ``-d/--device`` → ``[video].device``, ``-D/--audio-device`` →
    ``[audio].device``, ``--sid-model`` → ``[ultimate64].sid_model``,
    ``--system`` → ``[ultimate64].system``. ``$C64CAST_URL`` deliberately does
    NOT auto-save (explicit flags only).

    Merges onto the existing file (start from a machine-overlaid Config, apply
    this invocation's flags on top), writes it sparsely (only non-default
    fields) and atomically, prints the path + contents, and returns 0. If
    nothing savable was provided, prints what's savable and returns 2. The DMA
    password can never be written (``config_serialize`` suppresses it)."""
    from . import config_serialize, paths, transport
    from .connect import apply_to_config, parse_connection_uri

    provided = (
        args.url is not None
        or args.device is not None
        or args.audio_device is not None
        or args.sid_model is not None
        or args.system is not None
    )
    if not provided:
        log.error(
            "--save-settings: nothing to save. Provide at least one of: "
            "-u/--url (connection), -d/--device, -D/--audio-device, --sid-model, "
            "--system. Other fields: hand-edit %s (annotated TOML).",
            paths.settings_path(),
        )
        return 2

    # Start from the existing file's values so a save merges rather than
    # replaces (defaults → existing machine settings → this invocation).
    cfg = cfgmod.Config()
    cfgmod.apply_machine_settings(cfg)

    if args.url is not None:
        apply_to_config(cfg, parse_connection_uri(args.url))
    if args.device is not None:
        cfg.video.device = args.device
    if args.audio_device is not None:
        cfg.audio.device = args.audio_device
    if args.sid_model is not None:
        cfg.ultimate64.sid_model = args.sid_model
    if args.system is not None:
        cfg.ultimate64.system = args.system

    text = config_serialize.dumps(cfg, minimal=True, schema_path=None)
    dest = paths.settings_path()
    transport.atomic_write_text(dest, text)
    print(f"Saved machine settings → {dest}\n")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def run_install_char_rom(path: str) -> int:
    """Install an existing character-ROM dump from `path`, then exit.

    Config-free and hardware-free — the fallback for a machine c64cast can't
    dump from (an emulator-only setup, a backend without read support, an
    exotic firmware). Returns 2 for an unreadable file or one that doesn't
    verify as a charset: both are user-fixable input problems, and writing an
    unverified file would poison every later run."""
    try:
        dest = char_rom.install(path)
    except OSError as e:
        log.error("--install-char-rom: could not read %s (%s)", path, e)
        return 2
    except ValueError as e:
        log.error("--install-char-rom: %s (%s)", e, path)
        return 2
    print(f"Installed the character ROM → {dest}")
    print(f"  {char_rom.verify(dest.read_bytes()).describe()}")
    return 0


def run_dump_char_rom(cfg: cfgmod.Config) -> int:
    """Read the character ROM off the connected C64 and cache it, then exit.

    Unconditional — re-dumping over an existing file is the entire point of the
    flag (the auto path only ever fires when nothing is installed). Resets the
    machine on the way out, like every other hardware-touching command, so it
    isn't left parked wherever the dump stub ran."""
    from c64cast.hw.backend import BackendCapabilityError

    be = make_backend(cfg)
    try:
        be.reset()
        time.sleep(1)
        be.run_basic_clear_loop()
        data = char_rom.dump(be)
    except BackendCapabilityError as e:
        log.error(
            "--dump-char-rom: this backend can't run the dump (%s). Install an "
            "existing dump instead: c64cast --install-char-rom PATH",
            e,
        )
        return 3
    except (OSError, RuntimeError) as e:
        log.error(
            "--dump-char-rom: %s. Check the machine is powered and responsive, "
            "or install an existing dump with --install-char-rom PATH.",
            e,
        )
        return 4
    finally:
        with contextlib.suppress(Exception):
            be.reset()
        be.close()

    dest = char_rom.install_data(data)
    print(f"Dumped the character ROM from the C64 → {dest}")
    print(f"  {char_rom.verify(data).describe()}")
    return 0


def run_calibrate_dac(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    """Measure the connected SID's Mahoney $D418 transfer curve and persist a
    per-system calibrated table (the --calibrate-dac command)."""
    if not AUDIO_AVAILABLE:
        log.error(
            "--calibrate-dac needs audio capture (sounddevice). Install the "
            "'mic' extra: uv tool install --force 'c64cast[all]'"
        )
        return 3
    # Resolve a name substring / index to a concrete input index (-1 → None
    # = system default). find_capture_device wants int | None.
    dev: int | None = None
    if args.audio_device is not None:
        idx = resolve_audio_input_device(args.audio_device)
        dev = idx if idx >= 0 else None
    be = make_backend(cfg)
    try:
        run = dac_calibration.run_calibration(
            be, cfg, device=dev, log_fn=lambda m: log.info("%s", m)
        )
    # A rig that can't be measured (no capture device, or a capture that
    # doesn't contain the ring) is a user-fixable setup problem, not a bug —
    # both carry actionable text, so print it and exit rather than traceback.
    except (CaptureUnavailableError, MeasurementError) as e:
        log.error("%s", e)
        return 3
    finally:
        be.close()
    # A run that measured every SID but trusted none of them still wrote a
    # file (raw levels, for diagnosis) — but it produced no usable table, so
    # it must not look like a success.
    if not any(r.sidtable is not None for r in run.entries.values()):
        log.error(
            "no usable DAC table was produced: every measured SID failed its "
            "volume-0 self-test. Playback keeps the existing curve. The raw "
            "levels were saved to %s for diagnosis.",
            run.path,
        )
        return 4
    return 0


def run_doctor(loaded: cfgmod.LoadResult, cfgs: list[cfgmod.Config]) -> int:
    """--doctor: validate + probe using the merged configs, so CLI flags
    (e.g. --skip-probe) and the C64CAST_DMA_PASSWORD env var take effect."""
    from .doctor import print_report, validate_load_result

    merged = cfgmod.LoadResult(
        cfgs=cfgs,
        names=loaded.names,
        paths=loaded.paths,
        is_ensemble=loaded.is_ensemble,
        master_control=loaded.master_control,
        master_midi_control=loaded.master_midi_control,
    )
    diagnostics = validate_load_result(merged, probe_u64=not cfgs[0].debug.skip_probe)
    return print_report(diagnostics)
