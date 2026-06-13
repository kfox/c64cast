"""Overlay framework.

An Overlay is a Scene decoration. The Playlist runs `setup()` after the
scene's setup, then `process_frame()` after each scene frame, and
`teardown()` before the scene's teardown. Multiple overlays stack in
declaration order — later overlays may obscure earlier ones in
overlapping cells.

Overlays write directly to fixed VIC/screen/color RAM addresses without
participating in the scene's delta-cache (they don't pass a region_id to
write_region). Restrictions are declared as class attributes and validated
at scene-build time in config.py.
"""
from __future__ import annotations

import difflib
import inspect
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from ..backend import C64Backend
    from ..modes import ComposeBuffers
    from ..scenes import Scene

log = logging.getLogger(__name__)

# Re-export common screen-code constants so overlays import once.
from ..backgrounds import SC_DOT as SC_DOT  # noqa: E402
from ..backgrounds import SC_FULL as SC_FULL  # noqa: E402
from ..backgrounds import SC_GT as SC_GT  # noqa: E402
from ..backgrounds import SC_HYPHEN as SC_HYPHEN  # noqa: E402
from ..backgrounds import SC_SPACE as SC_SPACE  # noqa: E402
from ..backgrounds import SC_STAR as SC_STAR  # noqa: E402


def ascii_to_screen(text: str) -> bytes:
    """Convert an ASCII string to C64 screen codes (uppercase character set).

    Same conversion the legacy TransitionScene used: PETSCII letters A-Z
    (0x40-0x5F) map to screen codes 0x00-0x1F; everything else passes
    through (works for digits, punctuation, space)."""
    return bytes(
        (ord(c) - 0x40) & 0x3F if 0x40 <= ord(c) <= 0x5F else ord(c) & 0xFF
        for c in text.upper()
    )


# ---------------------------------------------------------------------------
# Base + registry
# ---------------------------------------------------------------------------

class Overlay:
    name = "base"
    # One-line, author-facing description rendered by `--list-overlays` and
    # `--describe`. Subclasses should set this. Per-constructor-param help
    # lives in PARAM_HELP (merged across the MRO by the introspection layer,
    # so a subclass only needs to document the params it adds).
    HELP: str = ""
    PARAM_HELP: dict[str, str] = {}
    # When True, the overlay paints PETSCII screen codes into $0400/$D800.
    # The default character set + color RAM nibbles only render correctly
    # in the standard PETSCII display mode — MCM reinterprets color RAM
    # bit 3 as "this cell is multicolor", which munges both glyph spacing
    # (double-wide pixels) and color (low 3 bits only). Bitmap modes ($2000)
    # don't expose the character matrix at all. So we restrict to display
    # mode "petscii" rather than just "any char mode".
    REQUIRES_PETSCII = False
    REQUIRES_AUDIO = False
    # Optional whitelist of display-mode names this overlay supports. Empty
    # tuple = no restriction (works on any display mode that accepts it via
    # the other flags). Use this when an overlay is too custom to gate on
    # the generic REQUIRES_PETSCII / is_bitmapped flags — e.g. BigText only
    # makes sense on `blank` and `mcm`.
    COMPATIBLE_MODES: tuple[str, ...] = ()
    # When True, the overlay paints into the scene's screen/color buffers
    # via compose() before the scene pushes them. The Playlist's per-frame
    # process_frame() loop SKIPS this overlay because the scene already
    # invoked compose() on it during its render path. This is what prevents
    # flicker: scene + overlays produce one composed frame, uploaded once.
    PAINTS_INTO_BUFFERS = False
    # Flipped by the scene's render loop when compose() raises; once True the
    # Playlist skips this overlay for the rest of the scene so a broken
    # overlay doesn't spam errors every frame.
    _disabled: bool = False

    def setup(self, api: C64Backend, scene: Scene) -> None:
        pass

    def is_busy(self) -> bool:
        """Return True if this overlay is mid-operation and the Playlist
        should defer auto-advancing the scene (e.g. a long scrolling
        message that hasn't finished). Default False.

        The Playlist defers `scene.is_done = True` while any overlay
        reports busy — but a CTRL skip still cuts through immediately.
        """
        return False

    def compose(self, buffers: ComposeBuffers, scene: Scene, t: float) -> None:
        """Mutate `buffers` ('screen' and 'color', each a uint8 numpy array
        of length 1000) in place to paint this overlay.

        Only called for overlays with PAINTS_INTO_BUFFERS = True, and only
        when the scene's display mode supports_compose. Default no-op."""

    def process_frame(self, api: C64Backend, scene: Scene, t: float) -> None:
        """Per-frame work that writes directly to the U64 via `api` —
        VIC register pokes, etc.

        Overlays that paint into screen/color RAM should use compose()
        instead (set PAINTS_INTO_BUFFERS = True). The Playlist skips
        process_frame() for those so they don't race the scene write."""

    def teardown(self, api: C64Backend, scene: Scene) -> None:
        pass

    def cycle_style(self, api: C64Backend, scene: Scene) -> str | None:
        """Rotate this overlay to its next visual style. Triggered by the
        SHIFT key (via the Playlist) at the same time as the scene's
        display_mode is cycled. Return the new style label, or None when
        the overlay has no cyclable styles. Default: no-op.

        Informational overlays (clock, weather, callsign, countdown,
        network) should leave this alone — their colors are functional.
        Decorative overlays (big_text, marquee, spectrum, etc.) override
        this to rotate through a curated color cycle."""
        return None


_REGISTRY: dict[str, type[Overlay]] = {}

_OverlayT = TypeVar("_OverlayT", bound=Overlay)


def register(name: str) -> Callable[[type[_OverlayT]], type[_OverlayT]]:
    """Class decorator that registers an Overlay subclass under a config name.

    Returns the class unchanged so the decorated symbol keeps its concrete
    subclass type — important for static analyzers (Pyright/Pylance) to see
    each overlay's actual `__init__` parameters instead of the base class's.
    """
    def deco(cls: type[_OverlayT]) -> type[_OverlayT]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def known_overlays() -> list[str]:
    # Force-load submodules so all @register decorators have run.
    _load_all()
    return sorted(_REGISTRY)


def build_overlay(cfg: dict[str, Any], audio) -> Overlay:
    """Construct an Overlay from a config dict.

    cfg must contain a 'type' key. All other keys are passed to the overlay
    class's constructor (which validates them). `audio` is the shared
    AudioStreamer (may be None — overlays that REQUIRES_AUDIO will reject)."""
    _load_all()
    if "type" not in cfg:
        raise ValueError(f"overlay config missing 'type': {cfg!r}")
    type_name = cfg["type"]
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise ValueError(
            f"unknown overlay type {type_name!r} "
            f"(known: {', '.join(known_overlays())})"
        )
    if cls.REQUIRES_AUDIO and audio is None:
        raise ValueError(
            f"overlay {type_name!r} requires audio but audio is not enabled — "
            "enable [audio] in config or pass -A on the CLI"
        )
    # Filter 'type' out; pass remaining as kwargs.
    kwargs = {k: v for k, v in cfg.items() if k != "type"}
    # The overlay's __init__ accepts an `audio` kw for audio-using overlays.
    if cls.REQUIRES_AUDIO:
        kwargs.setdefault("audio", audio)
    try:
        return cls(**kwargs)
    except TypeError as e:
        hint = _kwarg_suggestion(cls, str(e))
        raise ValueError(
            f"overlay {type_name!r}: {e}.{hint} cfg={cfg!r}"
        ) from e


_BAD_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")


def _kwarg_suggestion(cls: type[Overlay], err_msg: str) -> str:
    """Turn a `__init__() got an unexpected keyword argument 'x'` TypeError
    into a ` (did you mean 'y'?)` hint by fuzzy-matching the bad key against
    the overlay's actual constructor parameters. Returns "" when there's no
    close match (or the TypeError wasn't an unknown-kwarg one)."""
    m = _BAD_KWARG_RE.search(err_msg)
    if not m:
        return ""
    bad = m.group(1)
    try:
        params = [p for p in inspect.signature(cls.__init__).parameters
                  if p not in ("self", "audio")]
    except (TypeError, ValueError):
        return ""
    close = difflib.get_close_matches(bad, params, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def validate_for_scene(overlay: Overlay, display_mode) -> None:
    """Raise ValueError if `overlay` can't run on a scene with `display_mode`."""
    mode_name = getattr(display_mode, "name", "?")
    if overlay.REQUIRES_PETSCII and not getattr(
            display_mode, "is_petscii_compatible", False):
        raise ValueError(
            f"overlay {overlay.name!r} paints PETSCII screen codes and only "
            f"renders correctly with display = 'petscii' or 'blank', but "
            f"this scene's display mode is {mode_name!r}. Change the scene "
            "to a compatible display mode or remove this overlay."
        )
    if overlay.COMPATIBLE_MODES and mode_name not in overlay.COMPATIBLE_MODES:
        modes = ", ".join(repr(m) for m in overlay.COMPATIBLE_MODES)
        raise ValueError(
            f"overlay {overlay.name!r} only renders on display modes "
            f"{{{modes}}}, but this scene's display mode is {mode_name!r}."
        )


_LOADED = False


def _load_all():
    """Import every overlay submodule once. The @register decorators populate
    the registry as a side effect."""
    global _LOADED
    if _LOADED:
        return
    # Import inside the function to avoid an import-time chain at package load.
    # Modules whose @register decorator depends on an optional dep (e.g.
    # obs_status → obsws-python) check the availability inside __init__
    # so just importing the module is always safe.
    from . import (  # noqa: F401
        big_text,
        callsign,
        clock,
        countdown,
        logo,
        marquee,
        network,
        obs_status,
        rss,
        scrolling_text,
        spectrum_petscii,
        weather,
    )
    _LOADED = True
