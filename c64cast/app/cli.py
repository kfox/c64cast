"""Command-line entry point.

CLI flags layer on top of a TOML config (``--config PATH`` or
``./c64cast.toml``). Precedence: built-in defaults < config file < CLI.
Every overridable option uses ``default=None`` so the merge step can tell
"user didn't pass it" from "user passed the default".

Flag parsing, the single-shot commands and config resolution live here; the
session itself — building each system's stack, running the playlists, tearing
it down — lives in :mod:`c64cast.app.session`, which ``_run_session`` composes.
The names this module re-exports below are that module's, kept importable from
``c64cast.app.cli`` because that is where callers have always found them.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from c64cast import UNINSTALLED_VERSION, __version__
from c64cast.hw.backend import make_backend  # noqa: F401 — re-export (diag scripts)

from . import config as cfgmod
from . import (
    orchestrators,  # noqa: F401 — registers built-in orchestrator subclasses
    paths,
    scene_factory,
    session,
)
from .cli_commands import (
    configure_logging,
    list_devices,
    run_calibrate_dac,
    run_doctor,
    run_dump_char_rom,
    run_install_char_rom,
    run_introspection,
    run_save_settings,
)
from .session import (  # noqa: F401 — re-exports; see the module docstring
    Session,
    SessionConfigError,
    StackBuildError,
    _build_audio,
    _build_input_controls,
    _build_preview_and_recording,
    _coerce_reu_for_backend,
    _coerce_reu_for_transport,
    _log_dma_setup_error,
    _maybe_save_live_tune,
    _open_backend,
    _pump_previews_until_done,
    _resolve_reu_available,
    _resolve_sampler_available,
    _run_playlists,
    build_stack,
    teardown_stack,
)

log = logging.getLogger("c64cast")

# The command's own name, in the parser and in `--version`'s output. Spelled
# once rather than as argparse's `%(prog)s`, which makes argparse %-format the
# whole version string — and a `%` in an install path (legal on Windows) would
# then raise on the way to the screen.
PROG = "c64cast"


class _CliUsageError(Exception):
    """A CLI-usage mistake (conflicting flags, a bad connection target).
    main() logs the message and returns exit code 2."""


def _device_arg(s: str) -> int | str:
    """argparse type for -d/--device and -D/--audio-device: an int index when it
    parses as one, else the raw string (a camera name substring / USB VID:PID, or
    an audio device name substring). Mirrors the int|str shape of
    [video].device / [audio].device; resolution happens later."""
    try:
        return int(s)
    except ValueError:
        return s


def _version_text() -> str:
    """`--version` output: the version, and the install it is running from.

    The path is the half that answers the question people actually ask. A
    release archive unpacked into a directory installs nothing, and
    ``__version__`` reads installed metadata — so "I upgraded and it still
    reports the old version" is nearly always the `PATH` command pointing at a
    different environment than the one that changed. Naming the directory the
    running code sits in says which environment that is, and names the installer
    that owns it (`uv/tools/…`, `pipx/venvs/…`) on the way past.
    """
    # site-packages, or the repo root for a checkout: two levels above
    # `c64cast/app/cli.py` is where the `c64cast` package itself sits.
    home = Path(__file__).resolve().parents[2]

    if __version__ == UNINSTALLED_VERSION:
        return f"{PROG} {__version__} (source checkout: {home})"
    return f"{PROG} {__version__} ({home})"


def build_parser() -> argparse.ArgumentParser:
    # Pull defaults from the config dataclasses so help text stays in sync
    # with the actual fallback values. CLI options use default=None at the
    # argparse layer so merge_cli() can distinguish "not provided" from
    # "explicitly set to the default"; the `(default: ...)` shown in --help
    # is the value the merge cascade lands on when nothing overrides it.
    u64_def = cfgmod.Ultimate64Cfg()
    video_def = cfgmod.VideoCfg()
    audio_def = cfgmod.AudioCfg()
    vision_def = cfgmod.VisionCfg()
    playlist_def = cfgmod.PlaylistCfg()
    debug_def = cfgmod.DebugCfg()
    web_def = cfgmod.WebCfg()

    p = argparse.ArgumentParser(
        prog=PROG,
        description="C64 AV streamer framework (Ultimate 64)",
    )

    p.add_argument("--version", action="version", version=_version_text())
    p.add_argument(
        "--config",
        default=None,
        help="Path to TOML config, or example:NAME for a packaged demo "
        "(see --list-examples) (default: ./c64cast.toml if it exists)",
    )

    p.add_argument(
        "inputs",
        nargs="*",
        metavar="MEDIA",
        help="Quick-playback media: files, directories, globs, or URLs played "
        "in order, once (no loop unless --loop). Each maps to a scene by kind: "
        "video->video, .sid->waveform, image->slideshow, .prg/.crt->launcher, "
        "URL->video. Omit to run from --config / ./c64cast.toml / defaults. "
        "Mutually exclusive with --config.",
    )

    conn = p.add_argument_group("connection")
    conn.add_argument(
        "-u",
        "--url",
        default=None,
        metavar="TARGET",
        help="Connection target selecting the hardware backend + endpoint "
        f"(default: $C64CAST_URL, else {u64_def.url}). Schemes: u64://HOST or "
        "http(s)://HOST (Ultimate 64 / II+); tr:// (TeensyROM+ USB serial, "
        "auto-detected), tr:///dev/cu.usbmodemXYZ or tr://COM3 (serial device), "
        "tr://HOST (TeensyROM+ TCP). Rare knobs as query params, e.g. "
        "u64://host?dma_port=64 or tr://host?tcp_port=2113.",
    )
    conn.add_argument(
        "-s",
        "--system",
        choices=["NTSC", "PAL"],
        default=None,
        help=f"Target system timing (default: {u64_def.system})",
    )
    conn.add_argument(
        "--sid-model",
        choices=list(cfgmod.SID_MODEL_CHOICES),
        default=None,
        help="Auto-configure the SID chip model per .sid PSID header, "
        "remapping to a matching physical socket or an UltiSID core if "
        f"needed ('off' disables) (default: {u64_def.sid_model})",
    )

    quick = p.add_argument_group("quick playback (with MEDIA args)")
    quick.add_argument(
        "--display",
        default=None,
        help="VIC-II display mode for quick-playback video/slideshow scenes (default: mhires).",
    )
    quick.add_argument(
        "-t",
        "--duration",
        type=float,
        default=None,
        help="Seconds for quick-playback scenes that honor it (waveform/slideshow).",
    )

    v = p.add_argument_group("video input")
    v.add_argument(
        "-d",
        "--device",
        type=_device_arg,
        default=None,
        metavar="INDEX|NAME|VID:PID",
        help="Webcam device: int index (-1 = system default), or a camera name "
        'substring / USB VID:PID (e.g. "Cam Link", "0fd9:0066"; needs the '
        f"'camera' extra) (default: {video_def.device})",
    )

    a = p.add_argument_group("audio")
    a.add_argument(
        "--audio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Stream audio to the 4-bit SID volume DAC; --no-audio mutes "
        f"(default: {audio_def.enabled})",
    )
    a.add_argument(
        "-D",
        "--audio-device",
        type=_device_arg,
        default=None,
        help="Audio input device: an int index (-1 = system default microphone), or a "
        f"device name substring (needs the 'mic' extra) (default: {audio_def.device})",
    )
    a.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=None,
        help=f"Audio sample rate in Hz (default: {audio_def.sample_rate})",
    )
    a.add_argument(
        "-m",
        "--mic-sensitivity",
        type=float,
        default=None,
        help=f"Microphone input gain multiplier (default: {audio_def.mic_sensitivity})",
    )
    a.add_argument(
        "-n",
        "--noise-gate",
        type=float,
        default=None,
        help=f"Threshold below which mic input is muted (default: {audio_def.noise_gate})",
    )
    a.add_argument(
        "--dac-calibration-profile",
        default=None,
        metavar="NAME|PATH",
        help="Override the auto-derived DAC calibration file key, for both "
        "--calibrate-dac and playback. A name keys a file under "
        "calibration/dac/profile-<name>.json, or names an existing file there "
        "as-is, e.g. the device-keyed 'ultimate-<id>' files --calibrate-dac "
        "writes (use when a TeensyROM+ moves between "
        "physical C64s: name each host's calibration once, reuse the name on every "
        "run there); a path (ending .json, or containing a separator) names a "
        "calibration file directly, which is how one machine's calibration is "
        f"reused from another backend (default: "
        f"{audio_def.dac_calibration_profile})",
    )

    vis = p.add_argument_group("vision input")
    vis.add_argument(
        "--vision",
        action="store_true",
        default=None,
        help="Enable webcam hand-gesture control "
        "(pinch=pause/resume, swipe=skip, open-hand=cycle); "
        f"needs the 'vision' extra (default: {vision_def.enabled})",
    )
    vis.add_argument(
        "--vision-model",
        default=None,
        help=f"Path to the MediaPipe HandLandmarker .task model (default: {vision_def.model_path})",
    )

    pl = p.add_argument_group("playlist")
    pl.add_argument(
        "--videos",
        default=None,
        help=f"Directory containing videos "
        f"({', '.join(scene_factory.VIDEO_EXTS)}) "
        f"(default: {playlist_def.videos_dir})",
    )
    pl.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Loop the playlist after the last scene finishes "
        "(--no-loop = exit after one pass; useful for "
        f'"play one video and quit") (default: {playlist_def.loop})',
    )

    web = p.add_argument_group("web console")
    web.add_argument(
        "--serve",
        action="store_true",
        default=None,
        help="Run the web console host instead of a one-shot session: an HTTP "
        "server that owns the hardware and starts/stops shows on request "
        f"(default bind {web_def.host}:{web_def.port}; configure under [web]; "
        "requires the 'web' extra). Prints a login URL carrying the shared token.",
    )

    intro = p.add_argument_group("introspection")
    intro.add_argument("--list-scenes", action="store_true", help="List scene types and exit")
    intro.add_argument("--list-overlays", action="store_true", help="List overlays and exit")
    intro.add_argument("--list-modes", action="store_true", help="List display modes and exit")
    intro.add_argument(
        "--describe",
        metavar="NAME",
        default=None,
        help="Describe a scene/overlay/section/mode and exit. "
        "Prefix to disambiguate: scene:, overlay:, "
        "section:, mode: (e.g. --describe overlay:clock)",
    )
    intro.add_argument(
        "--compat",
        action="store_true",
        help="Print the overlay × display-mode compatibility matrix and exit",
    )
    intro.add_argument(
        "--list-examples",
        action="store_true",
        help="List the example configs that ship with c64cast (run one with "
        "`--config example:NAME`) and exit",
    )
    intro.add_argument(
        "--print-example",
        metavar="NAME",
        default=None,
        help="Print a packaged example config to stdout and exit — redirect it "
        "to a file to make it yours (`--print-example hello > c64cast.toml`)",
    )
    intro.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the JSON Schema for the TOML config and exit "
        "(point your editor's `#:schema` at it for autocomplete)",
    )
    intro.add_argument(
        "--print-schema-path",
        action="store_true",
        help="Print where this install's JSON Schema lives — the value for a "
        "config's `#:schema` first line, worked out for `--config`'s location "
        "(default ./c64cast.toml) — and exit. Naming the installed copy is what "
        "makes the line outlive upgrades",
    )
    intro.add_argument(
        "--suggest-palette",
        metavar="FILE",
        default=None,
        help="Analyze an image or video and print the C64 colors that best "
        "represent it (ranked, faithful subset) for [color].force_palette_colors, "
        "then exit. No hardware.",
    )
    intro.add_argument(
        "--init",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Interactively build a config file (needs the "
        "'wizard' extra). Optional PATH sets the output "
        "file (default ./c64cast.toml)",
    )
    intro.add_argument(
        "--midi-setup",
        action="store_true",
        help="MIDI-learn wizard: press/twist your controller's buttons and "
        "knobs, then save a reusable controller profile (needs the 'midi' + "
        "'wizard' extras). A plain run then picks it up via "
        "[midi_control].controller_profile = 'auto'. No hardware target needed.",
    )
    intro.add_argument(
        "--save-settings",
        action="store_true",
        help="Persist this invocation's machine-relevant flags (-u/--url, "
        "-d/--device, --sid-model, --system) into the machine-settings file "
        "($C64CAST_SETTINGS, else ~/.config/c64cast/settings.toml), then exit. "
        "Merges with any existing file; secrets are never written.",
    )
    intro.add_argument(
        "--dump-char-rom",
        action="store_true",
        help="Read the character ROM out of the C64 you're connected to and "
        "cache it, then exit. C64 text then renders in the real C64 font "
        "instead of a built-in ASCII substitute. This normally happens by "
        "itself on the first run; use the flag to re-dump (e.g. after swapping "
        "in a different character ROM).",
    )
    intro.add_argument(
        "--install-char-rom",
        metavar="PATH",
        default=None,
        help="Install an existing character ROM dump (2 KB or 4 KB) from PATH "
        "instead of reading one off the C64, then exit. For machines c64cast "
        "can't dump from. No hardware needed.",
    )

    debug = p.add_argument_group("debug")
    debug.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=None,
        help="Increase log verbosity (default: INFO; -v enables DEBUG)",
    )
    debug.add_argument(
        "--heartbeat",
        type=float,
        default=None,
        help=f"Health heartbeat interval in seconds, 0 disables (default: {debug_def.heartbeat})",
    )
    debug.add_argument(
        "--skip-probe",
        action="store_true",
        default=None,
        help=f"Skip the startup U64 reachability probe (default: {debug_def.skip_probe})",
    )
    debug.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio and video input devices and exit",
    )
    debug.add_argument(
        "--doctor",
        action="store_true",
        help="Validate the whole config (all scenes/overlays at "
        "once), check optional extras + probe each U64, then "
        "exit. Add --skip-probe for a fast, offline, "
        "hardware-free config check.",
    )
    debug.add_argument(
        "--calibrate-dac",
        action="store_true",
        help="Measure the connected SID's Mahoney 8-bit $D418 DAC transfer curve "
        "(requires a capture device — Cam Link — on the SID audio output) and save "
        "a per-device calibrated table, then exit. On a U64/U2+, every populated "
        "physical SID socket is measured independently. Playback with "
        "[audio].dac_curve = 'auto' (the default) then uses the applicable table "
        "automatically. Most valuable for physical 6581/8580 chips and SID "
        "replacements, which vary chip-to-chip.",
    )
    debug.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Mirror log output to PATH (useful for headless runs)",
    )
    debug.add_argument(
        "--profile",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Emit per-scene frame timing summaries (cpu_render "
        "/ compose / push / wait, plus DMA writes/bytes per "
        f"frame) (default: {debug_def.profile})",
    )
    debug.add_argument(
        "--profile-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"Seconds between profiler summary lines (default: {debug_def.profile_interval})",
    )
    debug.add_argument(
        "--frame-numbers",
        action="store_true",
        default=None,
        help="Overlay playback timecode + source frame number on "
        "video frames (debug aid for locating flashing "
        f"frames) (default: {debug_def.frame_numbers})",
    )
    debug.add_argument(
        "--overwrite",
        action="store_true",
        help="On exit, silently save any live-tune parameter changes (made via "
        "MIDI/WLED during the run) back into the config's [color] section "
        "(keeping a .bak), instead of prompting. No effect if nothing changed "
        "or the run has no config file.",
    )
    return p


# CLI flags that don't make sense in ensemble mode (they pick a single
# system's hardware; the per-system TOML is the right place to set them).
_PER_SYSTEM_CLI_FLAGS: tuple[tuple[str, str], ...] = (
    ("url", "--url"),
    ("device", "--device"),
)


def _connection_is_builtin_default(cfg: cfgmod.Config) -> bool:
    """True when `cfg`'s connection fields still match a fresh dataclass Config
    — i.e. neither a CLI target nor machine settings supplied one. Lets the
    quick-playback "no target" warning fire only for the genuine built-in
    default and stay quiet when machine settings provided the connection."""
    d = cfgmod.Config()
    return (
        cfg.hardware.backend == d.hardware.backend
        and cfg.ultimate64.url == d.ultimate64.url
        and cfg.teensyrom.transport == d.teensyrom.transport
        and cfg.teensyrom.serial_port == d.teensyrom.serial_port
        and cfg.teensyrom.host == d.teensyrom.host
    )


def _log_unknown_keys(records: list[cfgmod.UnknownKey]) -> None:
    """Warn about stray TOML keys on the normal run path. `load_master` collects
    instead of logging so `--doctor` can render them as report rows; every other
    entry point still needs them on stderr, where an ignored key is the reason a
    setting "did nothing"."""
    for rec in records:
        log.warning("%s%s", rec.describe(), f" ({rec.hint})" if rec.hint else "")


def _resolve_configs(args: argparse.Namespace) -> tuple[cfgmod.LoadResult, list[cfgmod.Config]]:
    """Produce the per-system configs to run, from one of two front doors:

    * **Quick playback** — positional ``MEDIA`` args build an in-memory,
      single-system config (no TOML on disk), one scene per argument.
      Mutually exclusive with ``--config``.
    * **Config-driven** — ``--config`` / ``./c64cast.toml`` / built-in
      defaults, with CLI flags merged on top. An ``example:NAME`` target is
      rewritten to the packaged demo's real path *here*, before anything reads
      it, so the loader, the ensemble per-system resolver (which walks paths
      relative to the master file), ``--doctor`` and the live-tune write-back
      all stay prefix-unaware.

    The scheme-aware ``-u/--url`` target (or ``$C64CAST_URL``) is applied to the
    single system's connection fields in both single-system paths; in ensemble
    mode connection comes from the per-system TOMLs (per-system identity), so a
    CLI target there is rejected. Raises ``ConfigError`` (exit 5), or
    ``_CliUsageError`` / ``ValueError`` / ``RuntimeError`` (exit 2)."""
    args.config = paths.resolve_config_spec(args.config)
    if args.inputs:
        if args.config:
            raise _CliUsageError(
                "positional MEDIA arguments and --config are mutually exclusive "
                "— pass media for quick playback, or --config for a TOML playlist."
            )
        from . import quickcast

        cfg = quickcast.build_config(args)
        loaded = cfgmod.LoadResult(
            cfgs=[cfg],
            names=["cast"],
            paths=[None],
            is_ensemble=False,
            master_control=cfg.control,
            master_midi_control=cfg.midi_control,
        )
        return loaded, [cfg]

    loaded = cfgmod.load_master(args.config)
    if not args.doctor:
        # --doctor renders these as CONFIG rows in the report body instead;
        # logging here too would print each one twice, once as the preamble
        # noise this reporting exists to get away from.
        _log_unknown_keys(loaded.unknown_keys)

    # CLI flags apply to every per-system config. In ensemble mode reject the
    # flags that pick one system's hardware — `[ultimate64].url` (the -u target)
    # and `[video].device` are per-system identity and must come from the TOMLs.
    if loaded.is_ensemble:
        offending = [
            flag for dest, flag in _PER_SYSTEM_CLI_FLAGS if getattr(args, dest, None) is not None
        ]
        if offending:
            raise cfgmod.ConfigError(
                f"ensemble mode (`[ensemble]` in {args.config}) is incompatible "
                f"with per-system CLI flags: {', '.join(offending)}. Move these "
                "values into the per-system TOMLs."
            )

    cfgs = [cfgmod.merge_cli(c, args) for c in loaded.cfgs]
    # Scheme-aware connection target overrides the single system's connection
    # fields (env honored as a fallback). Ensemble systems keep their TOML
    # identity — the per-system-flag guard above already rejected a CLI target.
    if not loaded.is_ensemble:
        target = args.url or os.environ.get("C64CAST_URL")
        if target:
            from .connect import apply_to_config, parse_connection_uri

            apply_to_config(cfgs[0], parse_connection_uri(target))
    return loaded, cfgs


def _run_session(
    args: argparse.Namespace, loaded: cfgmod.LoadResult, cfgs: list[cfgmod.Config]
) -> int:
    """The playlist session, as the one-shot CLI runs it: validate, build,
    start the control surfaces, run to completion, tear down. The steps
    themselves live in :mod:`c64cast.app.session`; what this adds is the
    process-level wiring only a foreground CLI can do — signal handlers and
    the mapping from a failure back to an exit code.

    Everything before this — flag parsing + the single-shot commands — is
    main()'s job."""
    try:
        session.validate_configs(loaded, cfgs)
    except session.SessionConfigError as e:
        return e.exit_code

    try:
        sess = session.build_session(args, loaded, cfgs)
    except StackBuildError as e:
        return e.exit_code

    # SIGINT + SIGTERM -> graceful shutdown down the same path. Ctrl+C used to
    # ride the default handler's KeyboardInterrupt, which lands wherever the
    # main thread happens to be — including inside the teardown `finally`, where
    # a second impatient Ctrl+C would abandon the run's final reset and leave the
    # machine mid-session behind a traceback. A handler can't land mid-teardown,
    # and setting stop_event means an in-flight DMA finishes rather than being
    # cut (killing mid-DMA is what wedges the hardware into needing a power
    # cycle). The second signal restores the default disposition for whichever
    # one arrived — SIGINT or SIGTERM — rather than exiting on the spot, so a
    # third is what actually kills. A repeated SIGTERM (what a service manager
    # sends) needs this exactly as much as a repeated Ctrl+C does.
    # SIGHUP -> reload TOML config (only the [interstitial] + [playlist] +
    # [[scenes]] sections take effect; [audio], [video], [ultimate64] are
    # set at startup and reloading them would require restarting threads).
    #
    # Installed here rather than in session.py because signal.signal raises
    # ValueError off the main thread: a session built from a worker (a
    # long-lived host) must not inherit this.
    interrupted = False

    def _on_stop_signal(signum, _frame):
        nonlocal interrupted
        name = signal.Signals(signum).name
        if interrupted:
            log.warning("%s again; next one exits immediately (teardown may not finish)", name)
            signal.signal(signum, signal.SIG_DFL)
            return
        interrupted = True
        log.info("%s received; stopping", name)
        sess.stop_event.set()

    def _on_sighup(_signum, _frame):
        log.info("SIGHUP received")
        session.reload_all(sess)

    signal.signal(signal.SIGTERM, _on_stop_signal)
    signal.signal(signal.SIGINT, _on_stop_signal)
    # Windows has no SIGHUP, so config reload is POSIX-only (POST /reload on the
    # control plane is the portable equivalent). Keep the getattr: naming the
    # attribute directly fails pyright when it runs *on* Windows, where the name
    # is absent from the signal stubs and a hasattr() guard doesn't narrow it.
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        signal.signal(sighup, _on_sighup)

    try:
        session.start_services(sess)
        session.run_foreground(sess)
    finally:
        session.teardown_session(sess)

    for st in sess.stacks:
        log.info("[%s] %s stats: %s", st.name, st.api.profile.name, st.api.stats)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_devices:
        # Logging at default level; list-devices skips config load entirely.
        configure_logging(args.verbose or 0, args.log_file)
        return list_devices()

    # Introspection commands describe the config surface itself — no config
    # file, no hardware. Dispatch before load_master so they work anywhere.
    intro_rc = run_introspection(args)
    if intro_rc is not None:
        return intro_rc

    # --save-settings is a config-free command like the introspection ones:
    # it persists this invocation's machine-relevant flags and exits, never
    # touching hardware or loading a playlist.
    if args.save_settings:
        configure_logging(args.verbose or 0, args.log_file)
        return run_save_settings(args)

    # --install-char-rom is likewise config-free: it takes a file the user
    # already has and caches it, no hardware and no playlist involved.
    if args.install_char_rom is not None:
        configure_logging(args.verbose or 0, args.log_file)
        return run_install_char_rom(args.install_char_rom)

    try:
        loaded, cfgs = _resolve_configs(args)
    except cfgmod.ConfigError as e:
        # Logging may not be set up yet (verbose/log_file live in [debug]).
        # Set up a minimal default handler so the error reaches the user
        # whether or not they passed -v.
        configure_logging(args.verbose or 0, args.log_file)
        log.error("%s", e)
        return 5
    except (_CliUsageError, ValueError, RuntimeError) as e:
        configure_logging(args.verbose or 0, args.log_file)
        log.error("%s", e)
        return 2
    # Logging is process-wide; use the first stack's debug settings (they
    # already share defaults via the master cascade unless explicitly
    # overridden).
    configure_logging(cfgs[0].debug.verbose, cfgs[0].debug.log_file)

    # Quick-playback feedback: warn only when we're really on the built-in
    # default (no -u/env AND machine settings didn't supply a connection);
    # otherwise note the connection came from machine settings. Then log which
    # backend we resolved.
    if args.inputs:
        if not (args.url or os.environ.get("C64CAST_URL")):
            if _connection_is_builtin_default(cfgs[0]):
                log.warning(
                    "no connection target given (-u/--url or C64CAST_URL) — using "
                    "the built-in default %s. Point at your hardware with e.g. "
                    "-u u64://192.168.2.64 or -u tr://.",
                    cfgs[0].ultimate64.url,
                )
            else:
                log.info(
                    "no -u/--url given — using the connection from machine settings (%s backend)",
                    cfgs[0].hardware.backend,
                )
        log.info(
            "cast: %d scene(s) on the %s backend",
            len(cfgs[0].scenes),
            cfgs[0].hardware.backend,
        )

    if args.dump_char_rom:
        # Needs hardware, so it dispatches here (with configs resolved) rather
        # than up with the config-free commands. Single-system operation.
        if len(cfgs) > 1:
            log.warning(
                "--dump-char-rom operates on one system; dumping from the first (%s)",
                loaded.names[0],
            )
        return run_dump_char_rom(cfgs[0])

    if args.calibrate_dac:
        # Measure + persist the per-system DAC table, then exit.
        # Single-system operation.
        if len(cfgs) > 1:
            log.warning(
                "--calibrate-dac operates on one system; calibrating the first (%s)",
                loaded.names[0],
            )
        return run_calibrate_dac(cfgs[0], args)

    if args.doctor:
        return run_doctor(loaded, cfgs)

    # The web console replaces the process model rather than adding a surface
    # to it: the server starts first and owns every session that follows, so
    # `--serve` and `[web].enabled` are the same switch. [web] is process-wide
    # (like [control]), hence the master in ensemble mode.
    web_cfg = loaded.master_web if loaded.is_ensemble else cfgs[0].web
    if args.serve:
        web_cfg.enabled = True
    if web_cfg.enabled:
        from . import serve

        def load_for_serve(
            path: str | None,
        ) -> tuple[argparse.Namespace, cfgmod.LoadResult, list[cfgmod.Config]]:
            """Resolve one start's configs. The loader, not the loaded result:
            every start re-runs it, so a TOML edited while the host is up takes
            effect on the next show.

            A browser-chosen path gets its own copy of the namespace — the
            resolver reads (and rewrites) ``config`` on the one it is handed,
            and clobbering the launch namespace would redefine what a start
            with no path means for the rest of the host's life."""
            if path is None:
                return (args, *_resolve_configs(args))
            sub = argparse.Namespace(**vars(args))
            sub.config = path
            sub.inputs = []  # a named config isn't quick playback
            return (sub, *_resolve_configs(sub))

        return serve.run_daemon(web_cfg, load_for_serve, config_path=args.config or "")

    return _run_session(args, loaded, cfgs)


# How long ensure_exit gives lingering threads to finish on their own before
# forcing the issue. Generous enough that a thread mid-unwind still gets a
# clean exit (atexit handlers, flushed buffers); short enough that a truly
# stuck one doesn't keep the operator waiting twice.
FORCE_EXIT_GRACE_S = 5.0

# How often ensure_exit re-checks _lingering_threads() during the grace
# period. Mirrors session._JOIN_POLL_S: short enough to notice a thread
# finishing promptly, long enough not to spin.
_EXIT_POLL_S = 0.2


def _lingering_threads() -> list[threading.Thread]:
    """Non-daemon threads that will hold interpreter shutdown open."""
    main_thread = threading.main_thread()
    return [
        t for t in threading.enumerate() if t is not main_thread and not t.daemon and t.is_alive()
    ]


def ensure_exit(
    code: int,
    *,
    grace_s: float = FORCE_EXIT_GRACE_S,
    lingering: Callable[[], list[threading.Thread]] = _lingering_threads,
    hard_exit: Callable[[int], NoReturn] = os._exit,
) -> int:
    """Return ``code`` once nothing can stall the exit — or leave by force.

    Graceful teardown has already happened by the time this runs; every
    non-daemon thread still alive here is one that outlived its own deadline
    and was already logged as abandoned (see session.join_bounded and
    serve._Workers.join). `threading._shutdown()` would join it anyway,
    untimed and with no signal delivery, so a run that can't finish its own
    teardown would hang forever instead of releasing the machine. A thread
    that's merely mid-unwind still gets a clean exit: this only forces the
    issue once ``grace_s`` has passed with a survivor still standing.

    ``lingering`` is injected (rather than always reading the live process's
    threads) so a test can name exactly the threads it cares about instead of
    every non-daemon thread any other test happens to have left running in
    the same process. ``hard_exit`` is injected for the same reason every
    callable in serve.py is: a test asserts the call instead of dying — which
    is also why this still returns explicitly after calling it, even though
    the real ``os._exit`` never gets there."""
    deadline = time.monotonic() + grace_s
    stragglers = lingering()
    while stragglers and time.monotonic() < deadline:
        time.sleep(_EXIT_POLL_S)
        stragglers = lingering()

    if not stragglers:
        return code

    for t in stragglers:
        log.error("[%s] still running after teardown; forcing exit", t.name)
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    hard_exit(code)
    return code


def run(argv: list[str] | None = None) -> int:
    """The installed console-script entry point: ``main()`` plus the
    guarantee that the process actually ends (see ensure_exit)."""
    return ensure_exit(main(argv))


if __name__ == "__main__":
    sys.exit(run())
