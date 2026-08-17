"""One place that turns a live-tune target string into a write on a running scene.

A live-tune target is a string like ``mode.dither_strength`` or ``fx2.amount``:
a *holder* prefix naming an object hanging off the current scene, and the name
of a field that object's class declares in ``LIVE_PARAMS`` (a scalar, with a
range) or ``LIVE_CHOICES`` (a discrete list). :func:`introspect.live_targets`
enumerates every one of them from those same class attributes, so the set of
targets is the registries themselves and nothing here can offer a knob the code
does not have.

**Four surfaces turn these knobs and they used to resolve them three ways.**
``midi_control`` scaled a 0..127 CC, ``wled_device`` scaled a 0..255 slider, and
``perf_console`` walked ``scene.effects`` itself for the browser's effect rack —
each with its own copy of the holder lookup, two of them carrying a comment
saying they were kept mirrored by hand. This module is that lookup, once. The
surfaces differ only in what a value *means* to them, which is what :class:`Move`
carries: a controller reading and its full scale, a real value, or "step to the
next choice".

Two behaviors ride along with the resolution, and having them here is most of
the point of the extraction:

* **The OSD line.** A knob turned from a controller says so on the C64's screen;
  a knob turned from the web console must not, because that surface exists
  precisely so a performer has a readout the audience does not see. That is a
  per-surface decision (``Move.osd``), not a per-target one.
* **The tracker entry.** ``mode.<name>`` targets are the live face of config
  fields — ``[color]`` for the ones a whole show shares, a scene's own
  ``[[scenes]]`` block for ``palette_mode`` — so changing one records into the
  playlist's
  :class:`~c64cast.control.transport.LiveTuneTracker`, which a CLI run's exit
  offers to write back into the config. Every surface that reaches a ``mode.``
  target gets that by coming through here rather than by remembering to call it,
  and it is why the browser was routed through this module instead of being
  given a fifth copy of the lookup. (Note that ``serve.py`` tears sessions down
  with ``save_live_tune=False``: a daemon has no terminal to prompt on and must
  not rewrite show files on every stop, so under ``--serve`` the entry is
  recorded and nothing acts on it. Offering it there is a separate decision
  about the daemon, not about this seam.)

Everything here is a plain attribute write on an object the render loop reads
next frame: GIL-atomic, no DMA, no lock, safe to call from a MIDI reader thread,
an HTTP worker or the WLED listener. A target that does not resolve — no scene,
no such holder, no such declared param — is a **silent no-op** returning False,
because every caller is a control surface where the alternative is an exception
on a thread nobody is watching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c64cast.app.playlist import Playlist

#: A holder prefix addressing one layer of the effect chain: ``fx2`` or
#: ``effect[2]``. Mirrors ``config._FX_LAYER_HOLDER_RE`` (an independent copy so
#: ``config`` stays import-light — it is validated there, resolved here).
_FX_LAYER_HOLDER_RE = re.compile(r"^(?:fx(\d+)|effect\[(\d+)\])$")


class LiveTarget(NamedTuple):
    """A resolved target: the live object, and what its class says about the
    field. ``lo``/``hi`` are set for a scalar, ``choices`` for a choice."""

    holder: Any
    holder_attr: str
    name: str
    kind: Literal["scalar", "choice"]
    lo: float = 0.0
    hi: float = 1.0
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Move:
    """How a control surface wants a target moved.

    Say where to go exactly one way: ``value`` (the real number, or the choice
    by name — what a browser sends), ``position`` out of ``full_scale`` (a
    controller's raw reading — 127 for a MIDI CC, 255 for a WLED slider, 1.0 for
    an already-normalized web slider), or ``cycle`` to step a choice to the next
    one (what a pad tap means, since a momentary trigger has no position).

    ``osd`` is the surface's decision, not the target's: a controller wants the
    C64 to confirm what it just changed, the web console must not put a
    performer's edits on an audience-facing screen."""

    value: float | str | None = None
    position: float | None = None
    full_scale: float = 1.0
    cycle: bool = False
    osd: bool = True


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def resolve_holder(scene: Any, holder_attr: str) -> Any:
    """The object a holder prefix names on `scene`, or None.

    ``scene`` is the scene itself (the scope scenes mix in the renderer, so
    their params live on the scene rather than a source or an effect);
    ``mode`` is its display mode (the live color-pipeline knobs); ``fx<N>`` /
    ``effect[<N>]`` is one layer of the effect chain; anything else is a plain
    attribute (``source``, ``effect``)."""
    if scene is None:
        return None
    if holder_attr == "scene":
        return scene
    if holder_attr == "mode":
        return getattr(scene, "display_mode", None)
    layer = _FX_LAYER_HOLDER_RE.match(holder_attr)
    if layer is not None:
        idx = int(layer.group(1) if layer.group(1) is not None else layer.group(2))
        effects = getattr(scene, "effects", None) or []
        return effects[idx] if 0 <= idx < len(effects) else None
    return getattr(scene, holder_attr, None)


def resolve(scene: Any, target: str) -> LiveTarget | None:
    """`target` against the live objects of `scene`, or None if nothing there
    declares it. A holder that exists but does not declare the name is as
    unresolved as one that does not exist — the class attributes are the whole
    definition of what is tunable."""
    holder_attr, _, name = str(target).partition(".")
    holder = resolve_holder(scene, holder_attr)
    if holder is None or not name:
        return None
    params: dict[str, tuple[float, float]] = getattr(type(holder), "LIVE_PARAMS", {}) or {}
    if name in params:
        lo, hi = params[name]
        return LiveTarget(holder, holder_attr, name, "scalar", float(lo), float(hi))
    choices: dict[str, tuple[str, ...]] = getattr(type(holder), "LIVE_CHOICES", {}) or {}
    if name in choices:
        return LiveTarget(holder, holder_attr, name, "choice", choices=tuple(choices[name]))
    return None


def resolve_first(scene: Any, targets: Sequence[str]) -> LiveTarget | None:
    """The first of `targets` that resolves. What a surface with fixed controls
    and varying scenes needs: a WLED intensity slider means whichever of
    ``source.scale`` / ``effect.intensity`` / … the current scene actually has."""
    for target in targets:
        found = resolve(scene, target)
        if found is not None:
            return found
    return None


def read(scene: Any, target: str) -> float | str | None:
    """The value a target currently holds, or None if it does not resolve.
    What a state feed sends so a remote fader can start from where the knob is
    rather than snapping it to wherever the fader happened to be."""
    found = resolve(scene, target)
    if found is None:
        return None
    return current(found)


def current(found: LiveTarget) -> float | str | None:
    """The value behind an already-resolved target. Choices are read through
    the holder's own ``get_live_choice`` — the stored attribute is not always
    the choice string (a palette mode owns more state than its name)."""
    if found.kind == "choice":
        getter = getattr(found.holder, "get_live_choice", None)
        return None if getter is None else getter(found.name)
    value = getattr(found.holder, found.name, None)
    return float(value) if isinstance(value, (int, float)) else None


def norm_of(found: LiveTarget, value: float | str | None) -> float:
    """Where `value` sits in a scalar target's range, 0..1. Zero for a choice
    or a value that isn't a number — a slider position is only meaningful for
    a scalar."""
    if found.kind != "scalar" or not isinstance(value, (int, float)):
        return 0.0
    span = found.hi - found.lo
    return _clamp01((float(value) - found.lo) / span) if span else 0.0


def apply(pl: Playlist, target: str, move: Move) -> bool:
    """Move one target on `pl`'s current scene. False if nothing was written."""
    found = resolve(pl.current, target)
    return False if found is None else apply_to(pl, found, move)


def apply_first(pl: Playlist, targets: Sequence[str], move: Move) -> bool:
    """:func:`apply` against the first of `targets` that resolves."""
    found = resolve_first(pl.current, targets)
    return False if found is None else apply_to(pl, found, move)


def apply_to(pl: Playlist, found: LiveTarget, move: Move) -> bool:
    """Move an already-resolved target. Split from :func:`apply` for the caller
    that resolved in order to decide *whether* to offer the control at all."""
    if found.kind == "scalar":
        return _apply_scalar(pl, found, move)
    return _apply_choice(pl, found, move)


def _apply_scalar(pl: Playlist, found: LiveTarget, move: Move) -> bool:
    new = _scalar_value(found, move)
    if new is None:
        return False
    old = getattr(found.holder, found.name, None)
    setattr(found.holder, found.name, new)
    if move.osd:
        pl.post_osd(f"{found.name} {new:.2f}")
    _record(pl, found, old, new)
    return True


def _scalar_value(found: LiveTarget, move: Move) -> float | None:
    """Where a Move puts a scalar, clamped into the declared range. None when
    the Move says nothing a scalar can use (a choice name, or a bare cycle)."""
    if move.position is not None and move.full_scale:
        return found.lo + _clamp01(move.position / move.full_scale) * (found.hi - found.lo)
    if isinstance(move.value, (int, float)):
        return max(found.lo, min(found.hi, float(move.value)))
    return None


def _apply_choice(pl: Playlist, found: LiveTarget, move: Move) -> bool:
    """Choices are set through the holder's ``set_live_choice`` rather than by
    assignment: picking a palette mode rebuilds state the attribute alone does
    not carry, and the helper returns the OSD label to show for it."""
    setter = getattr(found.holder, "set_live_choice", None)
    if setter is None or not found.choices:
        return False
    old = current(found)
    chosen = _chosen(found, move, old)
    if chosen is None:
        return False
    # The scene owns the backend handle; a mode that has to repaint on a choice
    # change needs it, and one that doesn't ignores the argument.
    label = setter(getattr(pl.current, "api", None), found.name, chosen)
    if move.osd:
        pl.post_osd(label or f"{found.name} {chosen}")
    _record(pl, found, old, chosen)
    return True


def _chosen(found: LiveTarget, move: Move, old: float | str | None) -> str | None:
    """Which choice a Move selects: the next one along (a pad tap), the named
    one (a browser), or the one a controller's position buckets into."""
    choices = found.choices
    if move.cycle:
        at = choices.index(old) if isinstance(old, str) and old in choices else -1
        return choices[(at + 1) % len(choices)]
    if isinstance(move.value, str):
        return move.value if move.value in choices else None
    if move.position is not None and move.full_scale:
        norm = _clamp01(move.position / move.full_scale)
        return choices[min(len(choices) - 1, int(norm * len(choices)))]
    return None


def _record(pl: Playlist, found: LiveTarget, old: Any, new: Any) -> None:
    """File a ``mode.<name>`` change into the playlist's live-tune tracker for
    the save-back. Only mode params have a config field behind them; effect /
    source / scene params are transient runtime state, so tracking them would
    write knob positions into a show file.

    The scene playing when the knob moved goes in with it, because not every mode
    field's home is the shared ``[color]`` section — ``palette_mode`` belongs to
    one ``[[scenes]]`` block. The tracker decides whether that matters for the
    target at hand; here it is enough to say what was on screen."""
    if found.holder_attr == "mode":
        scene = getattr(pl.current, "cfg_index", None)
        pl.live_tracker.record(f"mode.{found.name}", old, new, scene=scene)
