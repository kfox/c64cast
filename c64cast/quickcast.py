"""Quick-playback config builder.

Build an **in-memory-only** :class:`~c64cast.config.Config` from a list of
file / directory / glob / URL arguments — no TOML on disk. One scene per
argument, in the order given, no video interleaving, no loop (override with
``--loop``). This is the library behind ``c64cast``'s positional ``MEDIA``
mode: when :func:`c64cast.cli.main` sees positional arguments (and no
``--config``) it calls :func:`build_config` here, then runs the result through
the normal path (:func:`c64cast.cli.build_stack` → ``_run_playlists`` →
``teardown_stack``); it adds no new playback machinery.

Argument → scene type mapping:

* video file (``.mp4`` …) → ``video``
* ``.sid``                → ``waveform``
* image (``.jpg`` …)      → ``slideshow``
* ``.prg`` / ``.crt``     → ``launcher``
* directory / glob        → the single scene type its contents imply
  (the dir/glob spec is passed straight through, so the scene random-picks
  at setup — "a directory of SIDs plays a random SID")
* URL                     → ``video`` (direct media URLs play as-is;
  YouTube and other sites are resolved via the optional ``yt-dlp`` extra)

Audio-only files (``.mp3`` …) are recognized but **not yet supported** (a
test-pattern-over-audio scene is a planned follow-up); they raise a clear
message rather than a generic "unknown file type".
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import urllib.parse

from .config import (
    PICTURE_EXTS,
    PROGRAM_EXTS,
    SID_EXTS,
    VIDEO_EXTS,
    Config,
    SceneCfg,
    apply_machine_settings,
)

log = logging.getLogger(__name__)


class _YtDlpLog:
    """Absorb yt-dlp's own console output at debug level.

    ``YoutubeDL.trouble()`` writes error/warning text straight to stderr
    unconditionally (ignoring the ``quiet``/``no_warnings`` options) unless a
    ``logger`` is supplied. Extraction failures are re-raised as a clean
    ``ValueError`` by :func:`resolve_media_url`, so that raw text would
    otherwise print — undithered, possibly ANSI-colored — ahead of (and
    duplicating) our own message.
    """

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


# Audio-only formats. Recognized so the user gets a clear "deferred" message
# instead of "unknown file type" — audio-over-test-pattern is a planned
# follow-up (it needs a scene that doesn't require a video stream, which
# AVFileSource currently does).
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus")

# Extension group → scene type. The first match wins; groups are disjoint.
_GROUP_TO_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (VIDEO_EXTS, "video"),
    (SID_EXTS, "waveform"),
    (PICTURE_EXTS, "slideshow"),
    (PROGRAM_EXTS, "launcher"),
)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_GLOB_CHARS = re.compile(r"[*?\[]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Timestamp grammar for URL start offsets: bare seconds ("90", "90.5") or the
# YouTube [Nh][Nm][Ns] form ("90s", "1m30s", "1h2m3s", "1h"). At least one of
# h/m/s must be present for the unit form to match.
_TIMESTR_HMS_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)

# Display mode for video/slideshow scenes. mhires is the richest bitmap mode
# and suits arbitrary film/photo content (matches SceneCfg's own unset-display
# resolution for video, see config.resolve_scene_display); kept as an
# explicit constant here since quickcast also applies it to slideshow (whose
# SceneCfg default resolution differs — see config._resolve_slideshow_display)
# and needs a concrete value to fall back on when `-d/--display` isn't passed.
_DEFAULT_VIDEO_DISPLAY = "mhires"

# Scene types that accept a `duration_s` override from `-t/--duration`.
# Video rejects it (video-driven); launcher treats it as an idle timeout
# (surprising for a playback shortcut), so both are excluded.
_DURATION_TYPES = ("waveform", "slideshow")


def _is_url(arg: str) -> bool:
    """True if ``arg`` is an http(s) URL."""
    return bool(_URL_RE.match(arg))


def _parse_timestr(s: str) -> float | None:
    """Parse a timestamp string to seconds. Accepts bare seconds ("90",
    "90.5") and the [Nh][Nm][Ns] form ("90s", "1m30s", "1h2m3s"). Returns
    None for anything else (so an unparseable t= is ignored, not fatal)."""
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)  # bare seconds, e.g. "90" or "90.5"
    except ValueError:
        pass
    m = _TIMESTR_HMS_RE.match(s)
    if not m or not any(m.groups()):
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + sec)


def _parse_start_offset(url: str) -> float | None:
    """Start offset (seconds) from a media URL's timestamp, or None.

    Honors the `t` and `start` query params (in that order) and the `#t=`
    fragment — the forms YouTube and friends use for "start here" links. An
    unparseable or absent timestamp yields None (playback from the start)."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    for key in ("t", "start"):
        values = query.get(key)
        if values:
            offset = _parse_timestr(values[0])
            if offset is not None:
                return offset
    # `#t=90` / `#t=1m30s` fragment form.
    frag = parts.fragment
    if frag.lower().startswith("t="):
        return _parse_timestr(frag[2:])
    return None


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
    if scene_type in ("video", "slideshow"):
        scene.display = display or _DEFAULT_VIDEO_DISPLAY
    if duration_s is not None and scene_type in _DURATION_TYPES:
        scene.duration_s = duration_s
    return scene


def classify_local(arg: str, *, display: str | None, duration_s: float | None) -> SceneCfg:
    """Turn a local file / directory / glob argument into a SceneCfg. The
    original dir/glob spec is preserved as ``file`` so the scene re-resolves and
    random-picks at setup."""
    # An existing file wins over glob interpretation — filenames containing
    # `[`/`]`/`*`/`?` (e.g. YouTube-style `name [videoid].mp4`) would otherwise
    # be mistaken for glob patterns. Mirrors resolve_file_spec's ordering.
    if os.path.isfile(arg):
        scene_type = _scene_type_for_file(arg)
        return _make_scene(scene_type, arg, display=display, duration_s=duration_s)
    if os.path.isdir(arg):
        entries = [os.path.join(arg, f) for f in os.listdir(arg)]
        paths = [p for p in entries if os.path.isfile(p)]
        if not paths:
            raise ValueError(f"directory {arg!r} is empty")
        scene_type = _scene_type_for_paths(paths, label=f"directory {arg!r}")
        return _make_scene(scene_type, arg, display=display, duration_s=duration_s)
    if _GLOB_CHARS.search(arg):
        paths = [p for p in glob.glob(arg) if os.path.isfile(p)]
        if not paths:
            raise ValueError(f"glob {arg!r} matched no files")
        scene_type = _scene_type_for_paths(paths, label=f"glob {arg!r}")
        return _make_scene(scene_type, arg, display=display, duration_s=duration_s)
    # Non-existent literal path: classify by extension and let the scene's
    # setup() report a clear "file not found" if it's still missing at play time.
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

    Raises RuntimeError if a non-direct URL needs yt-dlp but it isn't installed,
    or ValueError if yt-dlp can't extract the media (unavailable/private/removed
    video, unsupported site, network failure, …) — both are plain "bad input"
    outcomes the caller reports as a clean message, not a stack trace.
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
        "logger": _YtDlpLog(),
        # Without this, YoutubeDL._format_err wraps "ERROR: " (and other
        # colored text) in raw ANSI escapes whenever it thinks its stderr is
        # a tty — which lands in the DownloadError message below regardless
        # of quiet/logger, and would otherwise leak into our own log output.
        "no_color": True,
    }
    try:
        # yt_dlp is an optional, untyped dependency — the call is dynamically typed.
        with yt_dlp.YoutubeDL(opts) as ydl:  # pyright: ignore[reportArgumentType]
            info = ydl.extract_info(url, download=False)
    # yt_dlp.DownloadError, not yt_dlp.utils.DownloadError (its defining
    # module) — a plain `import yt_dlp` doesn't pull in the `utils`
    # submodule, and yt_dlp's __init__ re-exports the exception itself.
    except yt_dlp.DownloadError as e:  # pyright: ignore[reportAttributeAccessIssue]
        # yt-dlp's own message is already prefixed "ERROR: " (see
        # YoutubeDL.report_error) — drop it so it doesn't double up with
        # ours. Also strip any ANSI escapes as a belt-and-braces guard,
        # since `no_color` should already prevent them above.
        reason = _ANSI_RE.sub("", str(e)).removeprefix("ERROR: ")
        raise ValueError(f"could not resolve {url!r}: {reason}") from e
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


def url_needs_ytdlp(url: str) -> bool:
    """True if a URL must be resolved by yt-dlp — i.e. it is NOT already a
    direct, playable media URL that PyAV can open. Mirrors the passthrough in
    :func:`resolve_media_url`. Offline (no network)."""
    path = urllib.parse.urlsplit(url).path.lower()
    return not path.endswith(VIDEO_EXTS + AUDIO_EXTS)


def _ytdlp_available() -> bool:
    """True if the optional yt-dlp dependency can be imported, without
    importing it. Used by config validation (and ``--doctor``) to flag a
    yt-dlp-requiring URL up front when the ``yt`` extra is missing."""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("yt_dlp") is not None


def resolve_video_url(url: str) -> tuple[str, float | None, str | None]:
    """Resolve a media URL for a **video** scene.

    Returns ``(stream_url, start_offset_s, title)``: the playable stream URL,
    the URL's ``t=``/``start=``/``#t=`` timestamp in seconds (None if absent),
    and the resolved title (None for direct URLs). Raises ValueError if the URL
    resolves to audio-only (deferred). Shared by quick playback and the config
    loader (:func:`c64cast.config.build_scene`) so both interfaces resolve URLs
    and honor timestamps identically."""
    stream_url, kind, title = resolve_media_url(url)
    if kind != "video":
        raise ValueError(
            f"{url!r} resolves to audio only, which isn't supported yet "
            "(test-pattern-over-audio is a planned follow-up)."
        )
    return stream_url, _parse_start_offset(url), title


def classify_url(arg: str, *, display: str | None) -> SceneCfg:
    """Turn a URL argument into a video SceneCfg.

    The URL is stored **verbatim**; it is resolved (yt-dlp) and audio-rejected
    later in :func:`c64cast.config.build_scene` — the single resolution path
    shared with config-driven runs. The ``t=``/``start=`` timestamp is parsed
    here (offline) so it rides onto the SceneCfg's ``start_s``."""
    scene = _make_scene("video", arg, display=display, duration_s=None)
    start_s = _parse_start_offset(arg)
    if start_s:
        scene.start_s = start_s
    return scene


def build_config(args: argparse.Namespace) -> Config:
    """Build the in-memory Config: defaults, plus one scene per positional
    ``MEDIA`` argument, plus the runtime overrides from the unified CLI flags.

    Called by :func:`c64cast.cli._resolve_configs` when the user passes
    positional media. ``args`` is the unified ``c64cast`` argparse namespace, so
    the connection target (``-u/--url`` or ``$C64CAST_URL``) is applied through
    the shared :mod:`c64cast.connect` decomposer — the same scheme-aware path
    the config-driven CLI uses — rather than being treated as a bare URL."""
    cfg = Config()
    # Machine settings (connection, capture device, SID model, …) are the
    # lowest layer, applied before the arg-driven field sets below — so quick
    # playback inherits them, and an explicit -u/--url still wins (it's applied
    # after). See config.apply_machine_settings.
    apply_machine_settings(cfg)
    # Each argument plays once, in order — no videos, no loop (unless --loop).
    cfg.playlist.loop = bool(args.loop)
    cfg.playlist.interleave_videos = False

    target = args.url or os.environ.get("C64CAST_URL")
    if target:
        from .connect import apply_to_config, parse_connection_uri

        apply_to_config(cfg, parse_connection_uri(target))
    if args.system:
        cfg.ultimate64.system = args.system
    if args.sid_model:
        cfg.ultimate64.sid_model = args.sid_model
    if args.device is not None:
        cfg.video.device = args.device
    # Audio is on by default in quick playback; --no-audio (args.audio == False)
    # mutes. args.audio is None when neither --audio nor --no-audio was passed.
    cfg.audio.enabled = True if args.audio is None else args.audio
    cfg.debug.skip_probe = bool(args.skip_probe)
    cfg.debug.verbose = args.verbose or 0
    cfg.debug.log_file = args.log_file

    scenes: list[SceneCfg] = []
    for arg in args.inputs:
        if _is_url(arg):
            scenes.append(classify_url(arg, display=args.display))
        else:
            scenes.append(classify_local(arg, display=args.display, duration_s=args.duration))
    cfg.scenes = scenes
    return cfg
