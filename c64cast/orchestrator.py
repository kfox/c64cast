"""Cross-ensemble scene coordination — the framework piece.

When a system's playlist enters a scene with `orchestrate = true`, that
system becomes a *conductor*; the ensemble's registry picks an
`Orchestrator` subclass that `claims()` the scene's shape and uses it
to interrupt every other system. Followers run a per-system *follower
scene* (looked up by name in their own playlist, or built from the
conductor's cfg as a fallback) until the conductor releases them.

Two patterns are supported through the same Orchestrator interface:

* **Span** (e.g. BigTextSpanOrchestrator): each follower
  renders a *slice* of the conductor's content. Followers' local
  SceneCfg overrides visual params; broadcast state (scroll position,
  glyph bits, ...) flows through `snapshot()`.

* **Mirror** (future): each follower renders the *same* content as the
  conductor in lockstep. Used for synchronized videos / SID
  playback / a webcam input only one system is wired to. Same protocol;
  the snapshot just carries different state (a master clock, a frame).

This module ships the ABC + a process-wide registry. Subclasses live
in `c64cast/orchestrators/` and register themselves with
`@register_orchestrator` at import time.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import SceneCfg
    from .ensemble import Ensemble


class OrchestratorError(Exception):
    """Raised when orchestrator selection or state is inconsistent —
    no subclass claims a scene, two subclasses claim it, or a subclass-
    specific invariant is violated (e.g. BigTextSpan's "conductor must
    be the rightmost system")."""


_REGISTRY: list[type[Orchestrator]] = []


def register_orchestrator(cls: type[Orchestrator]) -> type[Orchestrator]:
    """Decorator: register an Orchestrator subclass. Subclasses must be
    imported for their registration to take effect — `c64cast.cli`
    imports `c64cast.orchestrators` at startup to trigger every
    package-supplied subclass's registration."""
    _REGISTRY.append(cls)
    return cls


def registered_orchestrators() -> list[type[Orchestrator]]:
    """Snapshot of the registry. Useful in tests + diagnostics."""
    return list(_REGISTRY)


def resolve_orchestrator(scene_cfg: SceneCfg) -> type[Orchestrator]:
    """Find the orchestrator subclass that `claims()` this scene cfg.
    Raises OrchestratorError if zero or more than one subclass matches —
    both are configuration bugs we want surfaced at load time, not
    debugged from a broken broadcast."""
    matches = [cls for cls in _REGISTRY if cls.claims(scene_cfg)]
    if not matches:
        registered = [cls.__name__ for cls in _REGISTRY] or ["(none)"]
        raise OrchestratorError(
            f"no orchestrator subclass claims scene {scene_cfg.name!r} "
            f"(type={scene_cfg.type!r}, overlays="
            f"{[o.get('type') for o in scene_cfg.overlays]}). "
            f"Registered subclasses: {registered}"
        )
    if len(matches) > 1:
        names = [cls.__name__ for cls in matches]
        raise OrchestratorError(
            f"ambiguous orchestrator match for scene {scene_cfg.name!r}: "
            f"{names} all claim it. At most one must return True from "
            "claims()."
        )
    return matches[0]


class Orchestrator(ABC):
    """Cross-ensemble scene coordination base class.

    A subclass declares which scene shapes it handles via `claims()`,
    publishes content + position state via `snapshot()`, and may
    enforce subclass-specific invariants in the `_on_begin` hook
    (e.g. BigTextSpan rejects begin() when the conductor isn't the
    rightmost system).

    The base owns the interrupt/resume event plumbing per follower and
    the follower-scene-cfg resolution. Subclasses don't need to touch
    threading primitives directly."""

    @classmethod
    @abstractmethod
    def claims(cls, scene_cfg: SceneCfg) -> bool:
        """True if this subclass handles scenes of this shape. Called
        once per `orchestrate = true` scene at config-validation time
        (and again at conductor entry — keep it cheap and pure)."""

    def __init__(self, ensemble: Ensemble, conductor_name: str):
        self.ensemble = ensemble
        self.conductor_name = conductor_name
        self._lock = threading.Lock()
        self._active = False
        self._conductor_cfg: SceneCfg | None = None

    # ---- public protocol called from the conductor's scene/overlay ----

    def begin(self, conductor_cfg: SceneCfg) -> bool:
        """Open a broadcast. Returns True iff the orchestrator was idle
        and is now active; False if a broadcast was already running (the
        caller should fall back to running the scene locally so the
        conductor's playlist doesn't hang).

        Clears resume events from any previous broadcast cycle so the
        new cycle's `end()` is the one that fires them."""
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._conductor_cfg = conductor_cfg
            self._on_begin(conductor_cfg)
        # Clear any leftover resume events, then wake every follower
        # via the ensemble's per-system interrupt events. (Followers'
        # playlists each hold a reference to their own ensemble event,
        # so the wake-up is delivered regardless of which orchestrator
        # instance owns this broadcast.)
        for name, ev in self.ensemble.broadcast_resume.items():
            if name != self.conductor_name:
                ev.clear()
        for name, ev in self.ensemble.broadcast_interrupt.items():
            if name != self.conductor_name:
                ev.set()
        return True

    def end(self) -> None:
        """Close a broadcast. Releases every follower (they tear down
        the follower scene and resume their saved playlist position).
        Idempotent — duplicate calls are no-ops."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._on_end()
            self._conductor_cfg = None
        for name, ev in self.ensemble.broadcast_resume.items():
            if name != self.conductor_name:
                ev.set()

    # ---- consumed by follower playlists ----

    def interrupt_event(self, name: str) -> threading.Event:
        """Event a follower playlist watches to know a broadcast started.
        Raises KeyError if `name` is the conductor (the conductor has
        no follower event)."""
        if name == self.conductor_name:
            raise KeyError(f"{name!r} is the conductor — no interrupt event for it")
        return self.ensemble.broadcast_interrupt[name]

    def resume_event(self, name: str) -> threading.Event:
        """Event a follower playlist waits on inside its broadcast loop.
        Raises KeyError if `name` is the conductor."""
        if name == self.conductor_name:
            raise KeyError(f"{name!r} is the conductor — no resume event for it")
        return self.ensemble.broadcast_resume[name]

    def is_active(self) -> bool:
        return self._active

    def follower_scene_cfg_for(self, follower_name: str) -> SceneCfg:
        """Pick the SceneCfg this follower should instantiate during the
        broadcast. If the follower's per-system TOML defines a scene
        with the same `name` (and `orchestrate = false` — we don't want
        two conductors), that local override wins so per-system visual
        params take effect. Otherwise return the conductor's cfg as a
        sensible default."""
        conductor_cfg = self._conductor_cfg
        if conductor_cfg is None:
            raise OrchestratorError("follower_scene_cfg_for called while orchestrator is inactive")
        follower_stack = self.ensemble.stack(follower_name)
        for sc in follower_stack.cfg.scenes:
            if sc.name == conductor_cfg.name and not sc.orchestrate:
                return sc
        return conductor_cfg

    # ---- subclass hooks (intentionally non-abstract — empty default
    #      lets subclasses opt in only when they have state to manage) ----

    def _on_begin(self, cfg: SceneCfg) -> None:  # noqa: B027
        """Called inside begin() under the lock, before follower events
        fire. Subclasses initialize per-broadcast state here. May raise
        OrchestratorError to refuse the broadcast (e.g. BigTextSpan
        when the conductor isn't the rightmost system) — the exception
        propagates through begin() so the conductor's playlist surfaces
        a clean error rather than starting a half-built broadcast."""

    def _on_end(self) -> None:  # noqa: B027
        """Called inside end() under the lock, before follower events
        fire. Subclasses clear per-broadcast state here."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return a dict of state followers consume to render their
        slice/mirror. Called from follower render threads; must be
        thread-safe (the base class's `_lock` is available if needed,
        but most subclasses can publish via the GIL alone given the
        small payload sizes)."""
