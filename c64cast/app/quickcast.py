"""Quick-playback config builder.

Build an **in-memory-only** :class:`~c64cast.app.config.Config` from a list of
file / directory / glob / URL arguments — no TOML on disk. One scene per
argument, in the order given, no video interleaving, no loop (override with
``--loop``). This is the library behind ``c64cast``'s positional ``MEDIA``
mode: when :func:`c64cast.app.cli.main` sees positional arguments (and no
``--config``) it calls :func:`build_config` here, then runs the result through
the normal path (:func:`c64cast.app.session.build_stack` → ``run_foreground``
→ ``teardown_stack``); it adds no new playback machinery.

Argument → scene type mapping:

* video file (``.mp4`` …) → ``video``
* ``.sid``                → ``waveform``
* image (``.jpg`` …)      → ``slideshow``
* ``.prg`` / ``.crt``     → ``launcher``
* audio file (``.mp3`` …) → ``generative`` + ``audio_source = "file"`` — the
  track plays on the DAC and a reactive plasma visual breathes with it
* directory / glob        → the single scene type its contents imply
  (the dir/glob spec is passed straight through, so the scene random-picks
  at setup — "a directory of SIDs plays a random SID")
* URL                     → ``video`` (direct media URLs play as-is;
  YouTube and other sites are resolved via the optional ``yt-dlp`` extra)

Two integration rules keep this front door equivalent to a config-driven run.
:func:`build_config` applies the machine-settings overlay
(``config.apply_machine_settings``) right after building the base Config and
before its own field sets, so quick playback inherits the saved connection
target / capture device / SID model without a per-run ``-u`` (an explicit
``-u`` / ``C64CAST_URL`` still wins, applied afterward). And CLI flags reach
the config through the shared ``config.merge_cli`` — quick playback sets only
its own *policy* first (no loop, no interleaved videos) so ``--loop`` still
overrides it. It must not hand-pick a subset of flags: it used to, and twelve
mapped flags plus ``C64CAST_DMA_PASSWORD`` were accepted and silently
dropped. ``tests/test_quickcast.py`` asserts against ``CLI_TO_CFG`` itself,
so a newly mapped flag is covered on the day it's added.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import logging
import os
import re
import urllib.parse

from .config import Config, SceneCfg, apply_machine_settings, merge_cli
from .scene_factory import AUDIO_EXTS, PICTURE_EXTS, PROGRAM_EXTS, SID_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ResolvedMedia:
    """One :func:`resolve_media_url` / :func:`resolve_video_url` result.

    A frozen object rather than a tuple so a field can be added without a
    positional-tuple widening — the tuple this replaces had already grown
    once, for `title`. `uploader`/`license`/`webpage_url` come from yt-dlp's
    info dict and stay `None` for a direct URL (nothing to extract) and for
    every field yt-dlp itself left blank — most sites, YouTube included,
    leave `license` empty even for licensed content, so `uploader` is
    offered alongside it as an attribution lead rather than a substitute."""

    stream_url: str
    kind: str  # "video" | "audio"
    title: str | None = None
    uploader: str | None = None
    license: str | None = None
    webpage_url: str | None = None
    # Set only by resolve_video_url, from the URL's own t=/start=/#t= timestamp.
    start_s: float | None = None


#: Per-socket-operation timeout for a URL resolution, in seconds. Bounds
#: `resolve_media_url`'s network fetch (see the ydl opts below).
URL_RESOLVE_TIMEOUT_S = 20.0


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


# Extension group → scene type; the first match wins and the groups are
# disjoint. "audio" is a sentinel `_make_scene` turns into a generative scene
# with audio_source = "file".
_GROUP_TO_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (VIDEO_EXTS, "video"),
    (SID_EXTS, "waveform"),
    (PICTURE_EXTS, "slideshow"),
    (PROGRAM_EXTS, "launcher"),
    (AUDIO_EXTS, "audio"),
)

# The generator an audio-file scene reacts with (a good all-round reactive look).
_AUDIO_GENERATOR = "plasma"
# A char mode keeps the 4-bit DAC path clean — no bitmap DMA competing with
# the audio ring.
_AUDIO_DISPLAY = "mcm"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_GLOB_CHARS = re.compile(r"[*?\[]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# The YouTube [Nh][Nm][Ns] start-offset form ("90s", "1m30s", "1h2m3s", "1h");
# bare seconds ("90", "90.5") are parsed separately.
_TIMESTR_HMS_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)

# Display mode for video *and* slideshow scenes when `-d/--display` isn't
# passed. Explicit rather than deferred because the two have different
# unset-display resolutions (config.resolve_scene_display versus
# scene_factory._resolve_slideshow_display).
_DEFAULT_VIDEO_DISPLAY = "mhires"

# Scene types that accept a `duration_s` override from `-t/--duration`. Video
# rejects it outright; launcher would read it as an idle timeout. "audio" honors
# it as a cap over the track's natural length.
_DURATION_TYPES = ("waveform", "slideshow", "audio")


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
    frag = parts.fragment
    if frag.lower().startswith("t="):
        return _parse_timestr(frag[2:])
    return None


def _type_for_ext(ext: str) -> str | None:
    """Scene type for a single extension (case-insensitive). Returns the sentinel
    ``"audio"`` for audio-only formats (mapped to a generative file scene by
    _make_scene), or ``None`` for an unrecognized extension."""
    ext = ext.lower()
    for exts, scene_type in _GROUP_TO_TYPE:
        if ext in exts:
            return scene_type
    return None


def _scene_type_for_file(arg: str) -> str:
    """Scene type for a single (literal) file argument. Raises ValueError with
    an actionable message for an unknown extension."""
    ext = os.path.splitext(arg)[1]
    scene_type = _type_for_ext(ext)
    if scene_type is None:
        known = ", ".join(VIDEO_EXTS + SID_EXTS + PICTURE_EXTS + PROGRAM_EXTS + AUDIO_EXTS)
        raise ValueError(f"{arg!r}: unknown file type {ext!r}. Supported: {known}")
    return scene_type


def _scene_type_for_paths(paths: list[str], *, label: str) -> str:
    """Single scene type implied by a collection of paths (a directory's
    contents or a glob's matches). Raises ValueError on empty, mixed, or
    unknown-only sets."""
    types: set[str] = set()
    for p in paths:
        t = _type_for_ext(os.path.splitext(p)[1])
        if t is not None:
            types.add(t)
    if len(types) == 1:
        return types.pop()
    if not types:
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
    where they're meaningful for that scene type. The ``"audio"`` sentinel builds
    a generative + audio_source = "file" scene (a plasma visual reacting to the
    decoded track)."""
    if scene_type == "audio":
        scene = SceneCfg(
            type="generative",
            source=_AUDIO_GENERATOR,
            file=file_spec,
            audio_source="file",
            display=display or _AUDIO_DISPLAY,
            name=name,
        )
        if duration_s is not None:
            scene.duration_s = duration_s
        return scene
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
    # An existing file wins over glob interpretation, so a name containing
    # `[`/`]`/`*`/`?` is not mistaken for a pattern. Mirrors resolve_file_spec.
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
    # A path that does not exist yet: classify by extension and let the scene's
    # setup() report "file not found" if it is still missing at play time.
    scene_type = _scene_type_for_file(arg)
    return _make_scene(scene_type, arg, display=display, duration_s=duration_s)


def resolve_media_url(url: str) -> ResolvedMedia:
    """Resolve a URL to a directly-playable media URL.

    Returns a :class:`ResolvedMedia` — ``kind`` is ``"video"`` or ``"audio"``,
    and ``uploader``/``license``/``webpage_url`` are filled from yt-dlp's info
    dict when available. Direct media URLs (path ends in a known media
    extension) pass through untouched — PyAV/ffmpeg opens http(s) directly, so
    only ``stream_url``/``kind`` are set. Everything else is resolved via
    yt-dlp (YouTube and every other site it supports), preferring a single
    *progressive* stream (combined audio+video in one container) because PyAV
    can't merge separate DASH streams without downloading — and 360/720p is
    ample for a 320x200 downscale.

    Raises RuntimeError if a non-direct URL needs yt-dlp but it isn't installed,
    or ValueError if yt-dlp can't extract the media (unavailable/private/removed
    video, unsupported site, network failure, …) — both are plain "bad input"
    outcomes the caller reports as a clean message, not a stack trace.
    """
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(VIDEO_EXTS):
        return ResolvedMedia(url, "video")
    if path.endswith(AUDIO_EXTS):
        return ResolvedMedia(url, "audio")

    try:
        import yt_dlp  # type: ignore[import-untyped]  # noqa: PLC0415  (lazy; optional extra)
    except ImportError as e:
        raise RuntimeError(
            f"playing {url!r} needs yt-dlp. Install the 'yt' extra: "
            "`uv tool install --force 'c64cast[all]'`."
        ) from e

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[vcodec!=none][acodec!=none]/best",
        "logger": _YtDlpLog(),
        # Bounded because this runs inside `build_scene`, after
        # `session.build_stack` has opened the link and reset the machine. With
        # no timeout, a host that completed the handshake and then never answered
        # held the C64 in reset until the process was killed, and left `serve.py`
        # stuck in STARTING where `POST /api/session/stop` could not end it.
        "socket_timeout": URL_RESOLVE_TIMEOUT_S,
        "retries": 2,
        # `YoutubeDL._format_err` wraps "ERROR: " in raw ANSI escapes whenever it
        # thinks its stderr is a tty, and those land in the DownloadError message
        # below regardless of quiet/logger.
        "no_color": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # pyright: ignore[reportArgumentType]
            info = ydl.extract_info(url, download=False)
    # `yt_dlp.DownloadError`, not `yt_dlp.utils.DownloadError` (its defining
    # module): a plain `import yt_dlp` does not pull in `utils`, and yt_dlp's
    # `__init__` re-exports the exception.
    except yt_dlp.DownloadError as e:  # pyright: ignore[reportAttributeAccessIssue]
        # `YoutubeDL.report_error` already prefixes "ERROR: "; dropping it keeps
        # ours from doubling up.
        reason = _ANSI_RE.sub("", str(e)).removeprefix("ERROR: ")
        raise ValueError(f"could not resolve {url!r}: {reason}") from e
    if info is None:
        raise ValueError(f"could not resolve media URL: {url}")
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
    return ResolvedMedia(
        stream_url,
        kind,
        title=info.get("title"),
        uploader=info.get("uploader"),
        license=info.get("license"),
        webpage_url=info.get("webpage_url"),
    )


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


def resolve_video_url(url: str) -> ResolvedMedia:
    """Resolve a media URL for a **video** scene.

    Same :class:`ResolvedMedia` as :func:`resolve_media_url`, with ``start_s``
    filled from the URL's own ``t=``/``start=``/``#t=`` timestamp (None if
    absent). Raises ValueError if the URL resolves to audio-only (deferred).
    Shared by quick playback and the config loader
    (:func:`c64cast.app.config.build_scene`) so both interfaces resolve URLs
    and honor timestamps identically."""
    media = resolve_media_url(url)
    if media.kind != "video":
        raise ValueError(
            f"{url!r} resolves to audio only. Reactive audio playback is "
            "supported for local files (`c64cast tune.mp3`) but not yet for URLs."
        )
    return dataclasses.replace(media, start_s=_parse_start_offset(url))


def classify_url(arg: str, *, display: str | None) -> SceneCfg:
    """Turn a URL argument into a video SceneCfg.

    The URL is stored **verbatim**; it is resolved (yt-dlp) and audio-rejected
    later in :func:`c64cast.app.config.build_scene` — the single resolution path
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

    Called by :func:`c64cast.app.cli._resolve_configs` when the user passes
    positional media. ``args`` is the unified ``c64cast`` argparse namespace, so
    the connection target (``-u/--url`` or ``$C64CAST_URL``) is applied through
    the shared :mod:`c64cast.app.connect` decomposer — the same scheme-aware path
    the config-driven CLI uses — rather than being treated as a bare URL."""
    cfg = Config()
    # The lowest layer, applied before the arg-driven field sets below, so quick
    # playback inherits it and an explicit -u/--url still wins.
    apply_machine_settings(cfg)
    # Each argument plays once, in order. Set before merge_cli so `--loop` wins.
    cfg.playlist.loop = False
    cfg.playlist.interleave_videos = False

    # Every remaining CLI flag, through the same merge the config-driven path
    # uses — hand-picking a subset here silently drops the rest.
    merge_cli(cfg, args)

    target = args.url or os.environ.get("C64CAST_URL")
    if target:
        from .connect import apply_to_config, parse_connection_uri

        apply_to_config(cfg, parse_connection_uri(target))

    scenes: list[SceneCfg] = []
    for arg in args.inputs:
        if _is_url(arg):
            scenes.append(classify_url(arg, display=args.display))
        else:
            scenes.append(classify_local(arg, display=args.display, duration_s=args.duration))
    cfg.scenes = scenes
    return cfg
