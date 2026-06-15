"""One-shot playback shortcut.

Build an **in-memory-only** :class:`~c64cast.config.Config` from a list of
file / directory / glob / URL arguments and run it — no TOML on disk. One scene
per argument, in the order given, no ad interleaving, no loop (override with
``--loop``). This is a thin convenience layer over the normal run path
(:func:`c64cast.cli.build_stack` → ``_run_playlists`` → ``teardown_stack``); it
adds no new playback machinery.

Argument → scene type mapping:

* video file (``.mp4`` …) → ``commercial``
* ``.sid``                → ``waveform``
* image (``.jpg`` …)      → ``slideshow``
* ``.prg`` / ``.crt``     → ``launcher``
* directory / glob        → the single scene type its contents imply
  (the dir/glob spec is passed straight through, so the scene random-picks
  at setup — "a directory of SIDs plays a random SID")
* URL                     → ``commercial`` (direct media URLs play as-is;
  YouTube and other sites are resolved via the optional ``yt-dlp`` extra)

Audio-only files (``.mp3`` …) are recognized but **not yet supported** (a
test-pattern-over-audio scene is a planned follow-up); they raise a clear
message rather than a generic "unknown file type".

Invoked via ``scripts/cast.sh`` (``python -m c64cast.quickcast``).
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
import threading
import urllib.parse

from .config import (
    PICTURE_EXTS,
    PROGRAM_EXTS,
    SID_EXTS,
    VIDEO_EXTS,
    Config,
    ConfigError,
    SceneCfg,
)

log = logging.getLogger(__name__)

# Audio-only formats. Recognized so the user gets a clear "deferred" message
# instead of "unknown file type" — audio-over-test-pattern is a planned
# follow-up (it needs a scene that doesn't require a video stream, which
# AVFileSource currently does).
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus")

# Extension group → scene type. The first match wins; groups are disjoint.
_GROUP_TO_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (VIDEO_EXTS, "commercial"),
    (SID_EXTS, "waveform"),
    (PICTURE_EXTS, "slideshow"),
    (PROGRAM_EXTS, "launcher"),
)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_GLOB_CHARS = re.compile(r"[*?\[]")

# Display mode for video/slideshow scenes. mhires is the richest bitmap mode
# and suits arbitrary film/photo content; SceneCfg's default (hires_edges) is
# tuned for live-webcam Canny edges, not playback.
_DEFAULT_VIDEO_DISPLAY = "mhires"

# Scene types that accept a `duration_s` override from `-t/--duration`.
# Commercial rejects it (video-driven); launcher treats it as an idle timeout
# (surprising for a playback shortcut), so both are excluded.
_DURATION_TYPES = ("waveform", "slideshow")


def _is_url(arg: str) -> bool:
    """True if ``arg`` is an http(s) URL."""
    return bool(_URL_RE.match(arg))


def _type_for_ext(ext: str) -> str | None:
    """Scene type for a single extension (case-insensitive). Returns the
    sentinel ``"audio"`` for audio-only formats (deferred), or ``None`` for an
    unrecognized extension."""
    ext = ext.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    for exts, scene_type in _GROUP_TO_TYPE:
        if ext in exts:
            return scene_type
    return None


def _scene_type_for_file(arg: str) -> str:
    """Scene type for a single (literal) file argument. Raises ValueError with
    an actionable message for audio-only or unknown extensions."""
    ext = os.path.splitext(arg)[1]
    scene_type = _type_for_ext(ext)
    if scene_type == "audio":
        raise ValueError(
            f"{arg!r}: audio-only playback isn't supported yet "
            "(test-pattern-over-audio is a planned follow-up). "
            "Use a video file for now."
        )
    if scene_type is None:
        known = ", ".join(VIDEO_EXTS + SID_EXTS + PICTURE_EXTS + PROGRAM_EXTS)
        raise ValueError(f"{arg!r}: unknown file type {ext!r}. Supported: {known}")
    return scene_type


def _scene_type_for_paths(paths: list[str], *, label: str) -> str:
    """Single scene type implied by a collection of paths (a directory's
    contents or a glob's matches). Raises ValueError on empty, audio-only,
    mixed, or unknown-only sets."""
    types: set[str] = set()
    saw_audio = False
    for p in paths:
        t = _type_for_ext(os.path.splitext(p)[1])
        if t == "audio":
            saw_audio = True
        elif t is not None:
            types.add(t)
    if len(types) == 1:
        return types.pop()
    if not types:
        if saw_audio:
            raise ValueError(f"{label} contains only audio files, which aren't supported yet.")
        raise ValueError(f"{label} contains no playable files.")
    raise ValueError(
        f"{label} mixes scene types ({', '.join(sorted(types))}); "
        "point cast at a directory/glob of a single kind."
    )


def _make_scene(
    scene_type: str,
    file_spec: str,
    *,
    display: str | None,
    duration_s: float | None,
    name: str | None = None,
) -> SceneCfg:
    """Construct a SceneCfg, applying the display + duration overrides only
    where they're meaningful for that scene type."""
    scene = SceneCfg(type=scene_type, file=file_spec, name=name)
    if scene_type in ("commercial", "slideshow"):
        scene.display = display or _DEFAULT_VIDEO_DISPLAY
    if duration_s is not None and scene_type in _DURATION_TYPES:
        scene.duration_s = duration_s
    return scene


def classify_local(arg: str, *, display: str | None, duration_s: float | None) -> SceneCfg:
    """Turn a local file / directory / glob argument into a SceneCfg. The
    original dir/glob spec is preserved as ``file`` so the scene re-resolves and
    random-picks at setup."""
    if _GLOB_CHARS.search(arg):
        paths = [p for p in glob.glob(arg) if os.path.isfile(p)]
        if not paths:
            raise ValueError(f"glob {arg!r} matched no files")
        scene_type = _scene_type_for_paths(paths, label=f"glob {arg!r}")
        return _make_scene(scene_type, arg, display=display, duration_s=duration_s)
    if os.path.isdir(arg):
        entries = [os.path.join(arg, f) for f in os.listdir(arg)]
        paths = [p for p in entries if os.path.isfile(p)]
        if not paths:
            raise ValueError(f"directory {arg!r} is empty")
        scene_type = _scene_type_for_paths(paths, label=f"directory {arg!r}")
        return _make_scene(scene_type, arg, display=display, duration_s=duration_s)
    # Literal file. It needn't exist yet — the scene's setup() reports a clear
    # "file not found" if it's missing (mirrors resolve_file_spec's behavior).
    scene_type = _scene_type_for_file(arg)
    return _make_scene(scene_type, arg, display=display, duration_s=duration_s)


def resolve_media_url(url: str) -> tuple[str, str, str | None]:
    """Resolve a URL to a directly-playable media URL.

    Returns ``(stream_url, kind, title)`` where ``kind`` is ``"video"`` or
    ``"audio"``. Direct media URLs (path ends in a known media extension) pass
    through untouched — PyAV/ffmpeg opens http(s) directly. Everything else is
    resolved via yt-dlp (YouTube and every other site it supports), preferring a
    single *progressive* stream (combined audio+video in one container) because
    PyAV can't merge separate DASH streams without downloading — and 360/720p is
    ample for a 320x200 downscale.

    Raises RuntimeError if a non-direct URL needs yt-dlp but it isn't installed.
    """
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(VIDEO_EXTS):
        return url, "video", None
    if path.endswith(AUDIO_EXTS):
        return url, "audio", None

    try:
        import yt_dlp  # type: ignore[import-untyped]  # noqa: PLC0415  (lazy; optional extra)
    except ImportError as e:
        raise RuntimeError(
            f"playing {url!r} needs yt-dlp. Install with "
            "`uv sync --extra yt` (or `pip install c64cast[yt]`)."
        ) from e

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[vcodec!=none][acodec!=none]/best",
    }
    # yt_dlp is an optional, untyped dependency — the call is dynamically typed.
    with yt_dlp.YoutubeDL(opts) as ydl:  # pyright: ignore[reportArgumentType]
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"could not resolve media URL: {url}")
    # A playlist/page URL yields entries; take the first playable one.
    entries = info.get("entries")
    if entries is not None:
        playable = [e for e in entries if e]
        if not playable:
            raise ValueError(f"no playable entries at {url}")
        info = playable[0]
    stream_url = info.get("url")
    if not stream_url:
        raise ValueError(f"yt-dlp returned no stream URL for {url}")
    vcodec = info.get("vcodec")
    kind = "audio" if (vcodec is None or vcodec == "none") else "video"
    return stream_url, kind, info.get("title")


def classify_url(arg: str, *, display: str | None) -> SceneCfg:
    """Turn a URL argument into a commercial SceneCfg (the only supported URL
    kind today). Audio-only URLs raise the deferred-support message."""
    stream_url, kind, title = resolve_media_url(arg)
    if kind != "video":
        raise ValueError(
            f"{arg!r} resolves to audio only, which isn't supported yet "
            "(test-pattern-over-audio is a planned follow-up)."
        )
    return _make_scene("commercial", stream_url, display=display, duration_s=None, name=title)


def build_config(args: argparse.Namespace) -> Config:
    """Build the in-memory Config: defaults, plus one scene per input argument,
    plus the runtime overrides from the parsed flags."""
    cfg = Config()
    # Each argument plays once, in order — no ads, no loop (unless --loop).
    cfg.playlist.loop = args.loop
    cfg.playlist.interleave_ads = False

    if args.url:
        cfg.ultimate64.url = args.url
    if args.system:
        cfg.ultimate64.system = args.system
    if args.device is not None:
        cfg.video.device = args.device
    cfg.audio.enabled = args.audio
    cfg.debug.skip_probe = args.skip_probe
    cfg.debug.verbose = args.verbose

    scenes: list[SceneCfg] = []
    for arg in args.inputs:
        if _is_url(arg):
            scenes.append(classify_url(arg, display=args.display))
        else:
            scenes.append(classify_local(arg, display=args.display, duration_s=args.duration))
    cfg.scenes = scenes
    return cfg


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cast",
        description=(
            "Quick playback: one scene per file/URL argument, in order, no loop. "
            "video->commercial, .sid->waveform, image->slideshow, .prg/.crt->launcher, "
            "URL->commercial (yt-dlp resolves YouTube et al.)."
        ),
    )
    p.add_argument("inputs", nargs="+", help="files, directories, globs, or URLs to play")
    p.add_argument(
        "-u",
        "--url",
        default=os.environ.get("C64CAST_URL"),
        help="Ultimate 64 base URL (default: $C64CAST_URL, else the built-in default).",
    )
    p.add_argument("-s", "--system", choices=["NTSC", "PAL"], help="video system")
    p.add_argument("-d", "--device", type=int, help="webcam device index (rarely needed)")
    p.add_argument(
        "--no-audio",
        dest="audio",
        action="store_false",
        help="disable audio (audio is on by default).",
    )
    p.set_defaults(audio=True)
    p.add_argument(
        "--display",
        help="VIC-II display mode for video/slideshow scenes (default: mhires).",
    )
    p.add_argument(
        "-t",
        "--duration",
        type=float,
        help="seconds for scenes that honor it (waveform/slideshow).",
    )
    p.add_argument(
        "--loop",
        action="store_true",
        help="loop the playlist (default: play through once and exit).",
    )
    p.add_argument(
        "--skip-probe", action="store_true", help="skip the hardware reachability probe."
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug.")
    return p


def main(argv: list[str] | None = None) -> int:
    # Lazy import: the run path pulls in heavy/optional deps. Keeping it out of
    # module import means the pure classifiers stay importable (and testable)
    # without a hardware stack.
    from .cli import (
        StackBuildError,
        _run_playlists,
        build_stack,
        configure_logging,
        teardown_stack,
    )
    from .profiler import NullProfiler, set_profiler

    args = _build_parser().parse_args(argv)
    configure_logging(args.verbose)

    try:
        cfg = build_config(args)
    except (ValueError, ConfigError, RuntimeError) as e:
        log.error("%s", e)
        return 2

    profiler = NullProfiler()
    set_profiler(profiler)
    stop_event = threading.Event()

    try:
        stack = build_stack(cfg, "cast", args, stop_event=stop_event, profiler=profiler)
    except StackBuildError as e:
        return e.exit_code

    try:
        _run_playlists([stack], stop_event)
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    finally:
        teardown_stack(stack)

    log.info("u64 stats: %s", stack.api.stats)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
