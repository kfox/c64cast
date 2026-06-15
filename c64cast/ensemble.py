"""Multi-system runtime registry.

`SystemStack` is one U64 worth of runtime — api + audio + source +
playlist + key_poller + optional preview/recording. A single-system
invocation has one of these; ensemble mode holds a list of them.

`Ensemble` is the registry of all live systems plus a shared stop event
that brings every playlist's run loop down on Ctrl+C. The list is
ordered left-to-right, matching the physical screen arrangement — that
order is load-bearing for span-mode orchestrators (e.g. BigTextSpan
scrolls right-to-left, so the rightmost stack is the conductor).

In single-system mode `Ensemble` is `None`; the lone SystemStack is
constructed standalone and the existing single-system code paths read
unchanged. cli.py only allocates an Ensemble when [ensemble] is set in
the master TOML."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .backend import C64Backend
    from .config import Config
    from .framebuffer import Framebuffer
    from .keyboard import CommodoreKeyPoller
    from .orchestrator import Orchestrator
    from .playlist import Playlist
    from .preview import PreviewWindow, StreamRecorder
    from .video import WebcamSource
    from .vision import VisionController


@dataclass
class SystemStack:
    """One U64 worth of runtime: its API socket, audio streamer, scene
    source, playlist, keyboard poller, and optional preview/recording
    plumbing. A single-system invocation has exactly one of these;
    multi-system mode (ensemble) holds a list of them."""

    name: str
    cfg: Config
    api: C64Backend
    audio: AudioStreamer | None
    source: WebcamSource | None
    playlist: Playlist
    # None when the backend can't read C64 memory (e.g. TeensyROM) — there's
    # no physical-keyboard control surface; the control plane stands in.
    key_poller: CommodoreKeyPoller | None
    # None unless [vision].enabled — webcam hand-gesture control surface.
    vision_controller: VisionController | None = None
    # Startup probe verdict: True iff the U64's REU is Enabled. Resolves the
    # [video].use_reu_staged "auto" setting at scene-build time (including
    # SIGHUP/control-plane reloads + ensemble follower scenes, which rebuild
    # scenes after the initial probe). False under --skip-probe or a failed
    # query, so "auto" degrades to host-DMA rather than freezing video.
    reu_available: bool = False
    framebuffer: Framebuffer | None = None
    preview_window: PreviewWindow | None = None
    recorder: StreamRecorder | None = None


@dataclass
class Ensemble:
    """The set of systems running in this process, in left-to-right order.

    Construction is two-phase because every SystemStack needs `stop_event`
    threaded into its Playlist at build time: allocate the Ensemble with
    an empty `stacks` list and a fresh stop_event, build each stack
    against that stop_event, then assign the populated list to
    `ensemble.stacks` and call `populate_broadcast_events()` so the
    per-system Event objects exist before any Playlist starts polling.

    `active_orchestrator` is a single-slot field set by the conductor's
    Playlist._safe_setup when entering a scene with `orchestrate = true`
    and cleared by _safe_teardown when that scene exits. Follower
    playlists read this slot from inside _handle_broadcast_interrupt
    to find the in-flight orchestrator.

    `broadcast_interrupt` / `broadcast_resume` are per-system Event
    objects whose lifetime is the process. Each Playlist's
    _broadcast_interrupt / _broadcast_resume field references its own
    entry here (wired once at cli setup). Orchestrators just set/clear
    these events rather than allocating their own — that way a Playlist
    doesn't have to re-wire its event reference on every broadcast.

    `audio_holder` is the name of the system currently allowed to drive
    audio (video / waveform / midi). Only one system may hold it at
    a time; others whose playlist lands on an audio-bearing scene skip
    it until the holder releases. `audio_lock` guards the claim/release
    transaction so concurrent claims can't both win. Live scenes
    (webcam, blank) never claim — their audio is suppressed at build
    time in ensemble mode (see config.build_scene)."""

    stacks: list[SystemStack]
    stop_event: threading.Event
    active_orchestrator: Orchestrator | None = None
    broadcast_interrupt: dict[str, threading.Event] = field(default_factory=dict)
    broadcast_resume: dict[str, threading.Event] = field(default_factory=dict)
    audio_lock: threading.Lock = field(default_factory=threading.Lock)
    audio_holder: str | None = None

    def __post_init__(self) -> None:
        self.populate_broadcast_events()

    def try_claim_audio(self, name: str) -> bool:
        """Atomically claim the audio slot for `name`. Returns True if
        the caller is now the holder. A system re-claiming a slot it
        already holds counts as success — keeps repeat setup() calls
        (reload, follower→original restore) idempotent."""
        with self.audio_lock:
            if self.audio_holder is None or self.audio_holder == name:
                self.audio_holder = name
                return True
            return False

    def release_audio(self, name: str) -> None:
        """Release the audio slot if `name` currently holds it.
        Idempotent: mismatched or already-released calls are no-ops
        (logged at debug). Called from teardown paths that must not
        raise even on an inconsistent in-memory state."""
        with self.audio_lock:
            if self.audio_holder == name:
                self.audio_holder = None
            elif self.audio_holder is not None:
                log.debug(
                    "ensemble: %s tried to release audio slot held by %s — ignoring",
                    name,
                    self.audio_holder,
                )

    def populate_broadcast_events(self) -> None:
        """Allocate one interrupt + one resume Event per system. Idempotent
        — existing entries are kept (so cli can safely re-call after
        assigning `stacks` post-construction without resetting any state
        that might already be wired up)."""
        for s in self.stacks:
            self.broadcast_interrupt.setdefault(s.name, threading.Event())
            self.broadcast_resume.setdefault(s.name, threading.Event())

    def system_names(self) -> list[str]:
        """Ordered list of system names (left-to-right)."""
        return [s.name for s in self.stacks]

    def stack(self, name: str) -> SystemStack:
        """Look up a stack by name. Raises KeyError if unknown — never
        returns None, since downstream code is always operating on a
        name that came from `system_names()` or a validated config."""
        for s in self.stacks:
            if s.name == name:
                return s
        raise KeyError(f"unknown system {name!r}; known: {self.system_names()}")
