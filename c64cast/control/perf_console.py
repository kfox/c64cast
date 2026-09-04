"""Phone / web performance console (Live DJ/VJ Phase 5 — see
docs/architecture/control.md → "Live performance").

The no-OSD constraint (the C64 output is audience-facing) leaves the performer
with no on-screen readout of clip / effect / tempo state. Phase 4 fills that gap
with controller LEDs; this module is the other off-screen surface: a
phone-friendly touch page served by the **control plane** (same FastAPI app /
port as ``/status`` — `control_plane.build_app` registers these routes), with a
WebSocket live-state feed. It is the intended feedback surface for controllers
that can't light their pads (Arturia / SysEx-only grids — see the Phase-4 note).

Everything the console drives is the **same engine** the MIDI surface drives, so
a web launch and a pad launch are indistinguishable downstream:

* **Clip launch** enqueues a :class:`~c64cast.control.performance.ClipEvent` onto
  ``pl.performance`` (drained on the playlist thread) — never a scene mutation on
  this HTTP thread, the rule the whole performance path follows.
* **Tap tempo** calls ``pl.tempo.tap()`` — an in-memory beat-grid write, no DMA.
* **Effect bypass** flips ``scene.effects[i].enabled`` — a GIL-atomic bool the
  render loop reads next frame.
* **Live tune** goes through :mod:`live_tune`, the one module that resolves a
  target string against the running scene, so a knob turned here is the same
  write a MIDI CC or a WLED slider makes — including the live-tune tracker entry
  that lets a ``mode.*`` change be saved back into the config. That record rides
  back out in the state frame (:func:`_tuned_dict`), because a daemon has no exit
  prompt to offer it at; the write itself is an HTTP route in :mod:`web_api`,
  with the config store's other writers. The one thing this surface asks for
  differently is **no** ``post_osd``: performance feedback stays off the audience
  screen, which is the whole point of a phone console.
* **Transport** (``pause`` / ``resume`` / ``skip``) and **jump** set the same
  playlist events the C64's own keys do, so the run loop applies them at its
  next clean boundary rather than this thread mutating a scene. The Phase-7
  verbs (freeze / scrub / rw / ff / loop) reach the same ``TransportSession``
  the MIDI surface drives, and **that engine posts its own OSD line** — which
  is not a leak in the no-``post_osd`` rule above but the line the rule is
  drawn at: the audience screen carries transport **state** (``PAUSED``,
  ``PLAY``, ``LOOP 1:04-1:31``, ``REC ●`` beside its red border), because the
  picture is visibly doing that and an unexplained frozen frame is worse than a
  label. It does **not** carry confirmation that a control was pressed. That is
  why a ``loop_slot`` save no longer draws ``SAVED 3`` there: it changes a file
  on disk and nothing on screen, so it goes to the log — and to this console
  for free, since every pushed state frame carries ``loop_slots``
  (:func:`_transport_dict`), which is live feedback rather than a two-second
  flash. The rule is one boundary, applied in the engine, so the MIDI and web
  surfaces stay the mirror images they have been since Phase 2. What is *also*
  closed here is the caller's hand in those strings: the slot a console may
  name is bounded to ``JsonSlotStore.SLOT_MIN..SLOT_MAX`` (see
  :meth:`PerfBridge.transport`), so nothing caller-shaped is interpolated into
  an OSD line and nothing unbounded is persisted.
* **Looks** (Live DJ/VJ Phase 6) enqueue a :class:`~c64cast.control.performance.LookEvent`
  (``save`` / recall), drained on the playlist thread exactly like a clip launch —
  a look captures the active clip + effect-chain state and re-fires it on recall.

The controls are generated from the registries rather than listed here: the
effect rack from each live layer's own class ``LIVE_PARAMS``, and the tune panel
from :func:`introspect.live_targets` filtered to what the *current scene* can
actually be asked for. Neither can drift from the code, and neither can offer a
slider that writes nowhere.

Like :mod:`wled_device`, this module deliberately does **not** ``from __future__
import annotations``: the WebSocket route below annotates its param with a name
imported inside :func:`register_perf_routes`, and stringized annotations would
make FastAPI mis-read it as a query param and skip the WebSocket injection.
"""

import asyncio
import contextlib
import functools
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import PurePath
from typing import Any

from c64cast.app.playlist import Playlist

from . import live_tune
from .auth import BODY_TOO_LARGE_ERROR, BodyTooLarge, is_viewer, read_body, role_of, same_origin
from .performance import ClipEvent
from .transport import JsonSlotStore, TransportEvent

log = logging.getLogger(__name__)

#: The transport verbs a console may send.
#:
#: ``pause``/``resume``/``skip`` set the same playlist events the C64
#: keyboard and a MIDI transport button set — a machine-level halt, applied at
#: the run loop's next clean boundary (see :meth:`PerfBridge.transport`).
#:
#: ``freeze``/``unfreeze``/``rw``/``ff``/``seek``/``loop_toggle``/``loop_slot``
#: (Live DJ/VJ Phase 7) instead enqueue a
#: :class:`~c64cast.control.transport.TransportEvent` onto the current
#: scene's own :class:`~c64cast.scenes.video_transport.VideoTransportControls`
#: — pause-in-place with audio muting, not a machine halt — which is what the
#: Live tab's scrub bar, hold-to-rewind/fast-forward and A/B loop drive.
TRANSPORT_VERBS = (
    "pause",
    "resume",
    "skip",
    "freeze",
    "unfreeze",
    "rw",
    "ff",
    "seek",
    "loop_toggle",
    "loop_slot",
)

# How often the WebSocket pushes a fresh state snapshot to connected consoles.
# The beat grid advances continuously, so a client extrapolates the beat pulse
# locally between pushes (bpm + last beat_phase + wall-clock elapsed); this
# cadence only needs to be fast enough that clip/effect/tempo *changes* and the
# count-in readout feel live. ~3/sec is trivially cheap (one small JSON to a
# couple of phones) and nowhere near any I/O ceiling.
_PUSH_INTERVAL_S = 0.35

#: How many console state sockets one feed may hold open at once.
#:
#: Every accepted socket runs its own push loop, and every cycle of that loop
#: builds a whole state frame — the live-tune catalog resolved against the
#: running scene, plus the look store and the loop-preset store read off disk.
#: Nothing capped it: a handshake is a bare ``GET``, so the method gate admits
#: even a **viewer** token (the credential meant to be handed to a stranger),
#: and a couple of hundred `wscat` connections bought a few hundred frame
#: builds a second on the host that owns the hardware. This is the decision
#: ``web_api.MAX_SCREEN_WATCHERS`` / ``StreamSlots`` already made for
#: ``/api/screen/stream``: refuse past the cap rather than queue, because a
#: queued console connects and then shows nothing. Four browsers watching one
#: show is already unusual; eight is generous for the two feeds together.
MAX_CONSOLE_SOCKETS = 8

#: Cap on a ``POST /perf/command`` body. A console command is a few hundred
#: bytes, and this was the one POST in the package that skipped
#: :func:`auth.read_body` — ``await request.json()`` buffers every chunk before
#: parsing, unbounded, on a host that owns live hardware.
MAX_COMMAND_BYTES = 64 << 10

#: Longest live-tune target a console may name. Every real one is a short
#: dotted name (``mode.border``, ``fx2.amount``). The cap is here because
#: ``live_tune.resolve_holder`` parses the ``fx<n>`` prefix with ``int()`` and
#: CPython refuses an integer literal of more than 4300 digits, so a crafted
#: ``"fx" + "9" * 5000 + ".amount"`` raised ``ValueError`` from a place no
#: reader would think to guard.
MAX_TARGET_CHARS = 128

#: RFC 6455 close codes for a handshake this module refuses before accepting:
#: 1013 "try again later" for the socket cap, 1008 "policy violation" for a
#: cross-origin handshake (the code ``auth._deny`` uses for the same reason).
_WS_TRY_AGAIN_LATER = 1013
_WS_POLICY_VIOLATION = 1008


class SocketReader:
    """One long-lived ``receive_json`` on a console socket, polled per push.

    Shared with :func:`c64cast.control.web_api.register_web_routes`'s
    ``/api/ws``, which is the same push-then-poll loop over the same payload.

    **The receive is never cancelled.**
    ``asyncio.wait_for(websocket.receive_json(), timeout=…)`` cancels it every
    cycle, and that loses frames: ``receive_json`` awaits uvicorn's queue and
    then ``json.loads`` with no ``await`` between them, so a frame delivered in
    the same event-loop turn the timeout fires in finds ``_fut_waiter`` already
    resolved, gets ``_must_cancel`` set, and is thrown ``CancelledError``
    *after* the message has been popped off the queue. The frame is consumed
    and never returned, and ``except TimeoutError: continue`` made the loss
    silent — a pad tap or a ``{"session": "stop"}`` that does nothing, with
    nothing logged, on a host that owns live hardware. So the task is created
    once, waited on with a timeout, and left **pending** across a timeout for
    the next cycle; :meth:`close` is the one place a cancel is safe, because
    the loop is over by then.

    A frame that does not decode is reported as ``None`` rather than raised,
    which the callers' existing ``isinstance(msg, Mapping)`` guard already
    drops. That is the other half of this class's job: one stray frame used to
    close the console's only feed (see :meth:`poll`)."""

    def __init__(self, websocket: Any, *, label: str) -> None:
        self._ws = websocket
        self._label = label
        self._task: Any = None

    async def poll(self, timeout: float) -> tuple[bool, Any]:
        """``(arrived, frame)``, or ``(False, None)`` if nothing came in time.

        ``arrived`` with a ``None`` frame is a frame that did not decode: a text
        frame that isn't JSON raises ``JSONDecodeError`` and a **binary** one
        raises ``KeyError("text")``, neither of which is a
        ``WebSocketDisconnect``. Both used to reach the loop's outer handler and
        tear down the socket — the sole channel for session state and log
        lines — on one stray frame from a console build sending a ping, a stale
        bundle, or a ``wscat`` probe, and the decode happens *before* the
        read-only check, so a viewer could do it too. Compare ``web_api._body``,
        which maps exactly this input to a 400 and keeps serving.

        A disconnect still propagates, which is what ends the loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._ws.receive_json())
        done, _ = await asyncio.wait({self._task}, timeout=timeout)
        if not done:
            return False, None
        task, self._task = self._task, None
        try:
            return True, task.result()
        except (ValueError, KeyError, TypeError):
            log.debug("%s: ignoring an unparseable frame", self._label)
            return True, None

    async def close(self) -> None:
        """Cancel a still-pending receive on the way out of the loop."""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


def with_role(state: dict[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
    """Tag a console state frame with the caller's role, and redact what a
    viewer should not be handed. Shared by both feeds.

    The role is ``None`` when the server runs without a token. The page greys
    itself out for a ``viewer`` rather than letting taps fail silently against
    the 403 the auth middleware answers writes with.

    The redaction is ``tuned.config_path``. The absolute path of the running
    show file rode in every frame, and ``GET /perf/state`` and both socket
    pushes are read methods — so a **viewer** token, the read-only link this
    system is designed to hand to a guest, disclosed the operator's username
    and directory layout, which is reconnaissance for the config-store routes
    the same host exposes. No secret was in it, so this is disclosure and not
    escalation; it is emptied rather than dropped so the frame keeps one shape,
    and ``config_name`` (already on the wire) is all the page ever used it for
    — a truthiness test for whether a Save is offerable, which a viewer cannot
    do anyway."""
    state["role"] = role_of(scope)
    if is_viewer(scope):
        for system in state.get("systems", ()):
            if "config_path" in system.get("tuned", {}):
                system["tuned"]["config_path"] = ""
    return state


class ConsoleFeed:
    """The outbound half of a console state socket — the loop both ``/perf/ws``
    and :mod:`web_api`'s ``/api/ws`` run.

    :class:`SocketReader` extracted the inbound half and this one was left
    duplicated, which cost exactly what a duplicated loop costs: the two had
    already drifted (one kept a client registry the other didn't, one named a
    raise out of the dispatch in its handler comment and the other didn't), and
    every guard below would otherwise have had to be written twice and kept in
    agreement forever. The routes differ only in what goes *into* a frame and
    what a command frame dispatches to, which is what ``build_frame`` and
    ``dispatch`` are; ``on_tick`` is ``/api/ws``'s screen sweep, the one thing
    it does per cycle that isn't part of the frame.

    Three things the loop guarantees, so that neither caller has to remember
    them:

    * **The frame build and the dispatch run off the event loop.** Both do real
      blocking work — one frame reads the look store and the loop-preset store
      from disk, and a ``mode.border`` write is a DMA over TCP port 64 — and
      the loop they used to run on also serves ``/status``, every ``/api``
      route and the MJPEG screen stream, so a slow data dir or a stalled link
      stalled all of it. The sync ``perf_state`` route got the threadpool for
      free and was never affected, which was the tell.
    * **A command frame cannot end the feed.** :meth:`PerfBridge.apply`
      validates rather than coerces, *and* the dispatch has its own guard here,
      so the exception ladder below only ever sees the socket's own send and
      receive. That also stops ``except (ConnectionError, RuntimeError)`` —
      justified as "an abrupt client close" — from quietly swallowing a
      ``RuntimeError`` raised by an engine a console tap reached.
    * **The socket count is capped** (:data:`MAX_CONSOLE_SOCKETS`) and a
      cross-origin handshake is refused (:func:`auth.same_origin`), both before
      ``accept``. The registry that counts is the set that used to be
      write-only bookkeeping — the copied half of a broadcast registry with the
      broadcast left out, which invited the next contributor to assume a
      fan-out existed here."""

    def __init__(
        self,
        label: str,
        *,
        build_frame: Callable[[Mapping[str, Any]], dict[str, Any]],
        dispatch: Callable[[Mapping[str, Any]], Any],
        on_tick: Callable[[], None] | None = None,
        limit: int | None = None,
    ) -> None:
        self.label = label
        #: The sockets this feed is currently serving. Read as the cap.
        self.clients: set[Any] = set()
        self._build_frame = build_frame
        self._dispatch = dispatch
        self._on_tick = on_tick
        # Read at construction rather than bound as a default, so a test can
        # set the cap low without opening the real number of sockets.
        self._limit = MAX_CONSOLE_SOCKETS if limit is None else limit

    async def run(self, websocket: Any) -> None:
        """Serve one console socket — refused, or accepted and pushed to until
        it ends."""
        # Imported here for the reason `register_perf_routes` gives: this
        # module has to stay importable with no FastAPI in scope.
        from fastapi import WebSocketDisconnect

        refusal = self._refusal(websocket)
        if refusal is not None:
            code, why = refusal
            # Debug, not warning: a refusal is floodable by whoever caused it,
            # and this log is an appliance's only diagnostic surface.
            log.debug("%s: refusing a state socket (%s)", self.label, why)
            await websocket.close(code=code)
            return
        await websocket.accept()
        self.clients.add(websocket)
        # The one gap the auth middleware can't cover: a socket is a single
        # `GET` handshake, so inbound command frames have to be dropped here.
        read_only = is_viewer(websocket.scope)
        reader = SocketReader(websocket, label=self.label)
        try:
            # Push a fresh snapshot on a fixed cadence; the client extrapolates
            # the beat pulse locally in between. The polled receive lets a
            # client command frame (if any) through without blocking the push.
            while True:
                if self._on_tick is not None:
                    self._on_tick()
                # Split from the socket's own failures below: a state frame
                # that raises is *our* bug, and swallowing it silently leaves
                # every connected console waiting forever for a push that will
                # never come — a hang where an error belongs.
                try:
                    frame = await asyncio.to_thread(self._build_frame, websocket.scope)
                except Exception:
                    log.exception("%s: could not build a state frame", self.label)
                    break
                await websocket.send_json(frame)
                arrived, msg = await reader.poll(_PUSH_INTERVAL_S)
                if arrived and isinstance(msg, Mapping) and not read_only:
                    await self._apply(msg)
        except WebSocketDisconnect:
            pass
        except (ConnectionError, RuntimeError):
            # An abrupt client close surfaces as a transport error rather than
            # a `WebSocketDisconnect`, so these stay at debug — but everything
            # else below is a socket nobody asked to close.
            log.debug("%s: websocket closed", self.label, exc_info=True)
        except Exception:
            log.exception("%s: websocket closed unexpectedly", self.label)
        finally:
            await reader.close()
            self.clients.discard(websocket)

    def _refusal(self, websocket: Any) -> tuple[int, str] | None:
        """``(close code, why)`` for a handshake this feed will not accept."""
        if not same_origin(websocket.headers):
            return _WS_POLICY_VIOLATION, "cross-origin handshake"
        if len(self.clients) >= self._limit:
            return _WS_TRY_AGAIN_LATER, f"{self._limit} already open"
        return None

    async def _apply(self, msg: Mapping[str, Any]) -> None:
        """Dispatch one command frame off the loop, and never let it end the
        feed.

        The dispatch reaches every engine a console tap can touch, and none of
        them is audited against raising — while this socket is the console's
        only channel for state and log lines. So a raise from below is one
        debug line and then the next push, rather than a torn-down feed and a
        traceback per frame in ``--log-file``."""
        try:
            await asyncio.to_thread(self._dispatch, msg)
        except Exception:
            log.debug("%s: a command frame raised; ignoring it", self.label, exc_info=True)


def _tempo_dict(pl: Playlist) -> dict[str, Any]:
    """Snapshot the playlist's beat grid (all GIL-atomic reads). ``beat_phase`` /
    ``bar_phase`` are sampled once against a single ``now`` so the client's local
    extrapolation starts from a consistent instant."""
    tempo = pl.tempo
    now = time.monotonic()
    return {
        "bpm": round(float(tempo.bpm), 2),
        "running": bool(tempo.running),
        "source": tempo.source,
        "beats_per_bar": int(tempo.beats_per_bar),
        "beat_phase": tempo.beat_phase_at(now),
        "bar_phase": tempo.bar_phase_at(now),
    }


def _beats_remaining(pl: Playlist, detail: tuple[int, str, float, float]) -> float | None:
    """Beats until an armed clip's quantize boundary, for the count-in readout.
    ``None`` when the clock isn't running (a stopped clock launches at once, so
    there is no count-in); ``0`` for ``quantize = "off"`` (immediate)."""
    quantize, arm_beat, arm_bar = detail[1], detail[2], detail[3]
    tempo = pl.tempo
    if not tempo.running:
        return None
    if quantize == "off":
        return 0.0
    now = time.monotonic()
    if quantize == "beat":
        target = math.floor(arm_beat) + 1
        return max(0.0, target - tempo.beat_phase_at(now))
    # bar
    target_bar = math.floor(arm_bar) + 1
    remaining_bars = target_bar - tempo.bar_phase_at(now)
    return max(0.0, remaining_bars * tempo.beats_per_bar)


def _effects_dict(scene: Any) -> list[dict[str, Any]]:
    """One scene's effect chain as rack rows — one per layer, each with its
    bypass state, ``mod_source``, and every declared ``LIVE_PARAMS`` field
    (value + range + normalized position for the slider). Generated from the
    layer's own class ``LIVE_PARAMS`` (the registry source of truth), so the rack
    can't drift from the effects registry.

    Takes the scene rather than the playlist: :func:`_system_state` samples
    ``pl.current`` once and hands the same scene to every builder (see its
    docstring for what re-reading it cost)."""
    effects = getattr(scene, "effects", None) or []
    out: list[dict[str, Any]] = []
    for idx, eff in enumerate(effects):
        params: list[dict[str, Any]] = []
        live_params: dict[str, tuple[float, float]] = getattr(type(eff), "LIVE_PARAMS", {}) or {}
        for name, (lo, hi) in live_params.items():
            value = float(getattr(eff, name, lo))
            span = hi - lo
            norm = (value - lo) / span if span else 0.0
            params.append(
                {
                    "name": name,
                    "value": round(value, 4),
                    "min": float(lo),
                    "max": float(hi),
                    "norm": max(0.0, min(1.0, norm)),
                }
            )
        out.append(
            {
                "index": idx,
                "name": getattr(eff, "name", type(eff).__name__),
                "enabled": bool(getattr(eff, "enabled", True)),
                "mod_source": getattr(eff, "mod_source", "audio"),
                "params": params,
            }
        )
    return out


#: Every declared live-tune target, built once. It describes the registries,
#: not the run, so it cannot change while the process is up — and building it
#: pulls in numpy/cv2 through the mode and generator modules, which is work the
#: state feed does three times a second.
#:
#: ``None`` is the cold cache rather than an empty list, and the build is under
#: a lock. ``if not _LIVE_TARGETS`` could not tell "not yet built" from "the
#: registries yielded nothing" on the one path this comment says is read once,
#: and the unsynchronized rebind let two threadpool workers serving a cold
#: ``/perf/state`` both walk the whole model — the same honesty
#: ``web_api.api_introspect``'s cache took a lock for. (``live_targets()``
#: walks static class attributes a drift test pins, so the empty result is not
#: reachable today; the sentinel is what keeps this comment true if it becomes
#: reachable.)
_LIVE_TARGETS: list[Any] | None = None
_LIVE_TARGETS_LOCK = threading.Lock()


def _live_target_docs() -> list[Any]:
    global _LIVE_TARGETS
    with _LIVE_TARGETS_LOCK:
        if _LIVE_TARGETS is None:
            from c64cast.app import introspect

            _LIVE_TARGETS = introspect.live_targets()
        return _LIVE_TARGETS


def reset_live_target_docs() -> None:
    """Drop the cached catalog.

    For a test that alters the registries: a process-global with no reset hook
    let one test inherit whatever the previous test in the same worker had
    cached."""
    global _LIVE_TARGETS
    with _LIVE_TARGETS_LOCK:
        _LIVE_TARGETS = None


def _live_dict(scene: Any) -> list[dict[str, Any]]:
    """The live-tune knobs one scene actually has, with their values.

    Every declared target is tried against the running scene and only the ones
    that resolve are sent, so a console renders exactly what it can turn — a
    blank scene has no generator and a PETSCII scene has no dither, and neither
    should show a slider that writes nowhere. Grouping (`Color pipeline`,
    `Generator`, …) is ``introspect``'s, the same grouping the ``--midi-setup``
    picker offers, so the two surfaces name the same knob the same way.

    The per-layer effect knobs are *not* here — those are addressed by layer
    (``fx2.amount``) and have their own rack in :func:`_effects_dict`, where
    bypass lives too."""
    out: list[dict[str, Any]] = []
    for doc in _live_target_docs():
        found = live_tune.resolve(scene, doc.target)
        if found is None:
            continue
        value = live_tune.current(found)
        row: dict[str, Any] = {
            "target": doc.target,
            "group": doc.group,
            "name": found.name,
            "kind": found.kind,
            "value": value,
            "vocabulary": doc.vocabulary,
        }
        if found.kind == "scalar":
            row["min"] = found.lo
            row["max"] = found.hi
            row["norm"] = live_tune.norm_of(found, value)
        else:
            row["choices"] = list(found.choices)
        out.append(row)
    return out


def _tuned_dict(pl: Playlist) -> dict[str, Any]:
    """What has been *turned* since the show started, and whether it can be kept.

    :func:`_live_dict` is the knobs; this is the record of moving them. Every
    ``mode.*`` change files into the playlist's
    :class:`~c64cast.control.transport.LiveTuneTracker` whichever surface made it
    — a MIDI CC, a WLED slider, the C64's own menu, or this console — and a CLI
    run's exit is where that record is offered back to the config. A daemon has
    no exit to prompt at (``serve.teardown`` passes ``save_live_tune=False``, and
    must: a host that rewrote every show file it stopped would be unusable), so
    the record is what the browser is shown instead, and saving it is a
    deliberate tap rather than a question asked at the worst possible moment.

    ``savable`` is the count a Save button acts on, and it is not always
    ``len(changes)``: a knob no config field carries has nowhere to be written,
    and so does ``mode.palette_mode`` turned on a scene the config never named (a
    launched clip, an interleaved video) — its home is one ``[[scenes]]`` block
    and that scene has none. Those rows are still listed, because a change that
    will be lost at the end of the show is exactly what a performer needs told."""
    rows = pl.live_tracker.pending()
    savable = [r for r in rows if r["field"] is not None]
    out: dict[str, Any] = {
        "changes": rows,
        "savable": len(savable),
        # Whether there is a file to write to at all. A quick-playback run has
        # no config, and gets the same pasteable [color] block the CLI prints.
        "config_path": pl.config_path or "",
        # The file's bare name, no directory and no `.toml` — the same spelling
        # a config gets everywhere else in the console. No `ConfigStore` reaches
        # this surface, so a root-relative label (`config/journey`) isn't
        # available; the name alone is what both consoles show for a tune save.
        "config_name": (PurePath(pl.config_path).stem if pl.config_path else ""),
    }
    if savable and not pl.config_path:
        # From the rows already in hand. `toml_snippet()` calls `pending()` a
        # second time, and a knob turned between the two reads (a MIDI CC, a
        # second console) made `savable` and `snippet` describe different sets
        # — `savable > 0` paired with `snippet == ""` is exactly the condition
        # the page's `if (tuned.snippet)` branch keys off. `pending()`'s own
        # docstring promises this self-consistency for its rows.
        out["snippet"] = pl.live_tracker.snippet_from(savable)
    return out


def scene_rows(pl: Playlist, index: int | None = None) -> list[dict[str, Any]]:
    """The playlist's scenes, for a console that offers a jump.

    Shared with the control plane's ``/scenes`` so the two answers cannot
    disagree about what is playing. ``duration_s`` is None for a scene that runs
    until its source ends — VideoScene uses ``math.inf`` for that and JSON
    cannot carry it.

    ``index`` names which row is current, for a caller that has already sampled
    ``pl.index`` and needs these rows to agree with the rest of its snapshot
    (:func:`_system_state`); it defaults to reading it here."""
    current = pl.index if index is None else index
    return [
        {
            "index": i,
            "name": s.name,
            "duration_s": (None if math.isinf(s.duration_s) else s.duration_s),
            "is_current": i == current,
        }
        for i, s in enumerate(pl.scenes)
    ]


def _clip_state(slot: int, active: int | None, armed: int | None) -> str:
    if slot == active:
        return "active"
    if slot == armed:
        return "armed"
    return "loaded"


def _transport_dict(scene: Any) -> dict[str, Any] | None:
    """The DJ transport surface of one scene (Live DJ/VJ Phase 7) —
    ``None`` for a scene that declares none (a generator, a picture, a scope),
    which is what tells the console to render no transport bar rather than one
    that writes nowhere. Duck-typed against the same ``transport_*`` methods
    :class:`~c64cast.control.transport.TransportSession` dispatches onto, so a
    console can only ask for what the engine already exposes."""
    position = getattr(scene, "transport_position", None)
    duration = getattr(scene, "transport_duration", None)
    is_paused = getattr(scene, "transport_is_paused", None)
    if position is None or duration is None or is_paused is None:
        return None
    loop_info = getattr(scene, "transport_loop_info", None)
    loop_slots = getattr(scene, "transport_loop_slots", None)
    return {
        "position": round(position(), 2),
        "duration": duration(),
        "frozen": is_paused(),
        "loop": loop_info() if loop_info is not None else {"state": "none", "a": None, "b": None},
        "loop_slots": loop_slots() if loop_slots is not None else [],
    }


def _system_state(name: str, pl: Playlist) -> dict[str, Any]:
    """One system's whole console snapshot.

    ``pl.current`` and ``pl.index`` are sampled **once**, at the top, and
    handed down. Each of the four builders below used to re-read them, and
    ``Playlist._advance`` writes ``index`` and ``current`` as two separate
    statements with a teardown between them (``current`` is ``None`` for part
    of it) — so a scene advance interleaved with the frame build emitted one
    snapshot whose ``current_scene`` / ``scene_index`` / ``scenes[].is_current``
    described scene A while ``effects`` / ``live`` / ``transport`` described
    scene B, and the layer indices the console then offered addressed a chain
    that had already moved. ``LiveTuneTracker.pending``'s docstring writes the
    same discipline down for its own rows; nothing applied it here."""
    perf = pl.performance
    scene = pl.current
    index = pl.index
    active = perf.active_slot
    armed = perf.armed_slot
    detail = perf.armed_detail
    armed_block: dict[str, Any] | None = None
    if detail is not None:
        remaining = _beats_remaining(pl, detail)
        armed_block = {
            "slot": detail[0],
            "quantize": detail[1],
            "beats_remaining": (round(remaining, 2) if remaining is not None else None),
        }
    clips = perf.clips_info()
    for clip in clips:
        clip["state"] = _clip_state(int(clip["slot"]), active, armed)
    return {
        "name": name,
        "current_scene": scene.name if scene is not None else None,
        "scene_index": index,
        "paused": pl.pause_event.is_set(),
        "scenes": scene_rows(pl, index),
        "tempo": _tempo_dict(pl),
        "active_slot": active,
        "armed": armed_block,
        "clips": clips,
        "effects": _effects_dict(scene),
        # The color-pipeline / generator / scope knobs the current scene has.
        # The same list --midi-setup maps a controller onto, so a phone and a
        # MIDI box reach the same surface (Live DJ/VJ Phase 7).
        "live": _live_dict(scene),
        # …and what has already been turned, so a console can offer to keep it.
        "tuned": _tuned_dict(pl),
        # Saved look slots (Live DJ/VJ Phase 6) — the console lights a recall pad
        # only for a slot that holds a look. This reads the look store off
        # disk, and `transport.loop_slots` above reads the loop-preset store,
        # so building one frame does real blocking I/O — which is why
        # `ConsoleFeed` builds it on a thread rather than the event loop.
        "looks": perf.saved_look_slots(),
        # The current scene's DJ transport (freeze/scrub/rw/ff/A-B loop), or
        # None when it has none — see _transport_dict.
        "transport": _transport_dict(scene),
    }


def _as_int(cmd: Mapping[str, Any], key: str, *, default: int | None = None) -> int | None:
    """``cmd[key]`` as an ``int``, or ``None`` when it is absent or not a number.

    Every numeric field of a console command comes through here or
    :func:`_as_float`, because the caller is hand-written JS on a phone that may
    well be a cached page from an older build — see :meth:`PerfBridge.apply`.
    The exception set is wider than it looks: ``json.loads`` accepts the bare
    literals ``1e400`` / ``Infinity`` / ``NaN``, and ``int(float("inf"))``
    raises ``OverflowError`` (an ``ArithmeticError``, so not in the obvious
    ``(KeyError, TypeError, ValueError)`` tuple). A ``bool`` is refused because
    it is an ``int`` in Python and ``{"slot": true}`` would read as slot 1 —
    the same reason ``web_api._opt_index`` refuses one."""
    value = cmd.get(key, default)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(cmd: Mapping[str, Any], key: str, *, default: float | None = None) -> float | None:
    """``cmd[key]`` as a finite ``float``, or ``None``. See :func:`_as_int`;
    ``float("nan")`` and ``float("inf")`` parse without raising, so they are
    refused here rather than reaching a seek target or a slider position."""
    value = cmd.get(key, default)
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


class PerfBridge:
    """Read/write bridge between the web console and the per-system playlists.

    Takes a **provider** of the ensemble as an ordered ``[(name, Playlist)]``
    list (one system for a single-system run), called per read and per command:
    the console's server can outlive the session it drives (a host that starts
    and stops shows), so the set of systems is a moving target and an empty one
    just means nothing is running. Reads build the console state snapshot;
    writes go through the same performance engine the MIDI surface uses (clip
    launch → ``pl.performance.enqueue``, tap → ``pl.tempo.tap``, fx → a
    GIL-atomic layer write).

    **Not every method is cheap in-memory work**, which this docstring used to
    claim ("no DMA, no lock needed"). :meth:`state` reads the look store and
    the loop-preset store off disk per call, and :meth:`live` on
    ``mode.border`` / ``mode.background`` reaches
    ``BlankDisplayMode.set_border``, which is an ``api.write_regs("d020", …)``
    — a DMA write over TCP port 64, behind the render thread's per-command
    mutex, unboundedly long on a stalled link. That sentence mattered more than
    the latency did, because it is the one a contributor would trust when
    deciding where to call these from; both callers now go through
    :class:`ConsoleFeed`, which puts them on a thread.

    An idle console gets an empty ``systems`` list rather than the ``503`` the
    control-plane routes answer with: the page is the gig-day fallback surface
    and has to stay loadable and self-explanatory between shows."""

    def __init__(self, systems: Callable[[], list[tuple[str, Playlist]]]) -> None:
        self._systems = systems

    # -- reads ---------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        systems = self._systems()
        return {
            "multi": len(systems) > 1,
            "systems": [_system_state(name, pl) for name, pl in systems],
        }

    def _resolve(self, system: str | None) -> Playlist | None:
        """The target playlist for a command: the named system, or the first
        system when unnamed (the single-system common case). ``None`` when the
        name is unknown — or when no session is running at all."""
        systems = self._systems()
        if not systems:
            return None
        if system is None:
            return systems[0][1]
        return dict(systems).get(system)

    # -- writes --------------------------------------------------------------

    def launch(self, system: str | None, slot: int, pressed: bool = True) -> bool:
        """Fire (or release) a clip slot — enqueues a :class:`ClipEvent`, exactly
        as ``midi_control``'s ``clip_launch`` does. Returns False for an unknown
        system."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.performance.enqueue(ClipEvent(slot=slot, pressed=pressed))
        return True

    def tap(self, system: str | None) -> bool:
        """Register a tap-tempo hit on the target system's beat grid."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.tempo.tap(time.monotonic())
        return True

    def fx_bypass(self, system: str | None, layer: int, enabled: bool) -> bool:
        """Set effect-chain layer ``layer``'s bypass (``enabled``) on the current
        scene. A plain GIL-atomic bool write (the render loop reads it next
        frame); no OSD. Out-of-range layer / no chain → no-op, but a valid system
        still returns True (the command was addressed).

        The no-op is logged at debug. Not to change the documented True/False
        contract — the distinction between "no system" and "no such target" is
        deliberate — but because the page's ``post()`` discards every response
        body and never inspects ``ok``, so a pad that does nothing mid-set left
        no evidence on either side of the wire. One debug line is the only
        thing that answers "did the tap reach the host?" without a repro, and
        it costs nothing at default verbosity."""
        pl = self._resolve(system)
        if pl is None:
            return False
        effects = getattr(pl.current, "effects", None) or []
        if not 0 <= layer < len(effects):
            log.debug("perf console: system %r has no effect layer %d", system, layer)
            return True
        effects[layer].enabled = bool(enabled)
        return True

    def fx_param(self, system: str | None, layer: int, param: str, norm: float) -> bool:
        """Set a declared ``LIVE_PARAMS`` field of layer ``layer`` from a
        normalized ``0..1`` slider position. A silent no-op when the layer /
        param doesn't exist; no OSD, because this surface exists so a performer
        has a readout the audience does not."""
        return self.live(system, f"fx{int(layer)}.{param}", norm=norm)

    def live(
        self,
        system: str | None,
        target: str,
        *,
        norm: float | None = None,
        value: float | str | None = None,
    ) -> bool:
        """Turn any live-tune target on the current scene — the color pipeline,
        a generator, a scope, or one effect layer.

        ``norm`` is a slider position (0..1), ``value`` the real number or the
        choice by name; a picker sends the latter because a choice list has no
        meaningful position. Goes through :mod:`live_tune` exactly as the MIDI
        and WLED surfaces do, so a ``mode.*`` knob turned here records into the
        live-tune tracker exactly as a MIDI knob's would — and :func:`_tuned_dict`
        puts that record back on the wire, which is how a knob turned on a phone
        reaches the config the daemon has no exit prompt to offer it at. Returns
        False only for an unknown system: a target the current scene doesn't have
        is a no-op, not an error, because the scene can change between the frame
        that offered the control and the tap — logged at debug, because the page
        discards the response and a dead knob otherwise leaves no trace (see
        :meth:`fx_bypass`).

        The one refusal that is not about the system is an absurdly long
        ``target`` (:data:`MAX_TARGET_CHARS`), checked here because this is the
        single funnel both the ``live`` action and :meth:`fx_param` come
        through."""
        pl = self._resolve(system)
        if pl is None:
            return False
        if not target or len(target) > MAX_TARGET_CHARS:
            log.debug("perf console: refusing a %d-character live-tune target", len(target))
            return False
        move = (
            live_tune.Move(position=float(norm), full_scale=1.0, osd=False)
            if norm is not None
            else live_tune.Move(value=value, osd=False)
        )
        if not live_tune.apply(pl, target, move):
            log.debug("perf console: system %r cannot resolve %r right now", system, target)
        return True

    def transport(
        self,
        system: str | None,
        verb: str,
        *,
        pressed: bool = True,
        target: float | None = None,
        slot: int = 0,
        save: bool | None = None,
        clear: bool | None = None,
    ) -> bool:
        """Every transport verb (``TRANSPORT_VERBS``) on the target system.

        ``pause``/``resume``/``skip`` set the same events the C64's own keys
        and a MIDI transport button set, so the run loop applies them at its
        next clean boundary rather than mutating a scene from this thread.
        ``resume`` on a show that is not paused is harmless — the event is
        simply never consumed — so unlike the control plane's ``/resume``
        there is no conflict to report; a console showing a Pause button that
        is really a Resume button is the UI's problem, and it has the
        ``paused`` flag to solve it with.

        Everything else (Live DJ/VJ Phase 7 — freeze/scrub/rw/ff/loop) instead
        enqueues a :class:`~c64cast.control.transport.TransportEvent`, the same
        queue the MIDI transport surface drains from — see
        :class:`~c64cast.control.transport.TransportSession`. ``freeze``/
        ``unfreeze`` enqueue the target state rather than a bare toggle, and
        :meth:`~c64cast.control.transport.TransportSession._dispatch` checks
        ``transport_is_paused`` itself, on the playlist thread, right before
        acting on it — so two consoles open on the same show, or a network
        retry, can't race each other's stale read of the pre-enqueue state
        into a double-toggle that cancels out.

        ``loop_slot`` is the one verb here that **writes and deletes persisted
        state on disk** (``LoopPresetStore.save`` / ``delete``), so its ``slot``
        is bounded to ``JsonSlotStore.SLOT_MIN..SLOT_MAX`` — the range the look
        store has always enforced. An unbounded slot meant one new key
        persisted per event, each save rewriting the whole grown file on the
        playlist thread that drives the hardware, and the digits ended up in an
        OSD line over the audience output."""
        pl = self._resolve(system)
        if pl is None or verb not in TRANSPORT_VERBS:
            return False
        if verb in ("pause", "resume", "skip"):
            event = {"pause": pl.pause_event, "resume": pl.resume_event, "skip": pl.skip_event}[
                verb
            ]
            event.set()
            return True
        if verb in ("freeze", "unfreeze"):
            pl.transport.enqueue(TransportEvent(action=verb))
            return True
        if verb in ("rw", "ff"):
            pl.transport.enqueue(TransportEvent(action=verb, pressed=pressed))
            return True
        if verb == "seek":
            if target is None:
                return False
            pl.transport.enqueue(TransportEvent(action="seek", target=target))
            return True
        if verb == "loop_toggle":
            pl.transport.enqueue(TransportEvent(action="loop_toggle"))
            return True
        if verb == "loop_slot":
            if not JsonSlotStore.SLOT_MIN <= slot <= JsonSlotStore.SLOT_MAX:
                log.debug("perf console: loop slot %d is outside the pad range", slot)
                return False
            pl.transport.enqueue(
                TransportEvent(action="loop_slot", slot=slot, save=save, clear=clear)
            )
            return True
        # Unreachable while `TRANSPORT_VERBS` and the branches above agree, and
        # this line is what makes that a statement rather than an accident: the
        # dispatch used to *end* in the `loop_slot` enqueue with no `if`, so
        # adding a verb to the tuple — the obvious way to grow this surface,
        # and where a contributor starts — silently saved or cleared one of the
        # performer's loop presets instead. `tests/test_perf_console.py` walks
        # the tuple and asserts each verb has its own effect.
        return False

    def jump(self, system: str | None, index: int) -> bool:
        """Go to scene `index` now. A cut rather than an interstitial: a console
        jump is a correction ("that one, not this one"), and the transition
        would put a title card in front of the thing being corrected to.

        An index past the end is an addressed no-op (True), logged at debug for
        the reason :meth:`fx_bypass` gives."""
        pl = self._resolve(system)
        if pl is None:
            return False
        if not 0 <= index < len(pl.scenes):
            log.debug("perf console: system %r has no scene %d", system, index)
            return True
        pl.request_jump(index, skip_interstitial=True)
        return True

    def look(self, system: str | None, slot: int, save: bool) -> bool:
        """Save or recall a "look" (active clip + effect-chain state) on the
        target system — enqueues a :class:`~c64cast.control.performance.LookEvent`, drained
        on the playlist thread, exactly as ``midi_control``'s ``look_save`` /
        ``look_recall`` do. Returns False for an unknown system."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.performance.enqueue_look(slot, save=save)
        return True

    def apply(self, cmd: Mapping[str, Any]) -> bool:
        """Dispatch one console command dict (shared by the POST endpoints and
        the WS command frame). ``{"action": "launch"|"tap"|"fx"|"live"|
        "transport"|"jump"|"look", ...}``.

        **A decodable frame never raises.** Every field is read through
        :func:`_as_int` / :func:`_as_float` and a missing or unparseable one
        answers ``False``, the same as an unknown action. This used to index
        ``cmd["slot"]`` / ``["layer"]`` / ``["target"]`` / ``["index"]``
        directly and coerce with bare ``int()`` / ``float()``, so
        ``{"action": "launch"}`` raised ``KeyError`` from inside the WS push
        loop, escaped to its outer ``except Exception``, and closed the
        console's only feed — with a full traceback per bad frame at default
        verbosity, on a loop a caller can reconnect immediately. That is
        precisely the outcome :class:`SocketReader` exists to prevent for an
        *undecodable* frame, left open one layer down for a decodable one: the
        validation was enforced at the decode and defeated at the dispatch. The
        same body was an uncaught 500 on ``POST /perf/command``.

        It matters here rather than only at the call sites because the page
        that builds these frames is hand-written JS that nothing type-checks,
        and a cached phone page from a previous build is the expected skew on
        the surface whose whole job is to work when nothing else does."""
        action = cmd.get("action")
        system = cmd.get("system")
        if action == "launch":
            slot = _as_int(cmd, "slot")
            if slot is None:
                return self._malformed(cmd, "slot")
            return self.launch(system, slot, bool(cmd.get("pressed", True)))
        if action == "tap":
            return self.tap(system)
        if action == "fx":
            layer = _as_int(cmd, "layer")
            if layer is None:
                return self._malformed(cmd, "layer")
            if "param" in cmd:
                value = _as_float(cmd, "value", default=0.0)
                if value is None:
                    return self._malformed(cmd, "value")
                return self.fx_param(system, layer, str(cmd["param"]), value)
            return self.fx_bypass(system, layer, bool(cmd.get("enabled", True)))
        if action == "live":
            target = cmd.get("target")
            if not isinstance(target, str):
                return self._malformed(cmd, "target")
            # A slider sends `norm`, a picker sends `value` — the two are not
            # interchangeable and the key says which one this is.
            if cmd.get("norm") is None:
                return self.live(system, target, value=cmd.get("value"))
            norm = _as_float(cmd, "norm")
            if norm is None:
                return self._malformed(cmd, "norm")
            return self.live(system, target, norm=norm)
        if action == "transport":
            slot = _as_int(cmd, "slot", default=0)
            if slot is None:
                return self._malformed(cmd, "slot")
            save = cmd.get("save")
            clear = cmd.get("clear")
            return self.transport(
                system,
                str(cmd.get("verb", "")),
                pressed=bool(cmd.get("pressed", True)),
                target=_as_float(cmd, "target"),
                slot=slot,
                save=None if save is None else bool(save),
                clear=None if clear is None else bool(clear),
            )
        if action == "jump":
            index = _as_int(cmd, "index")
            if index is None:
                return self._malformed(cmd, "index")
            return self.jump(system, index)
        if action == "look":
            slot = _as_int(cmd, "slot")
            if slot is None:
                return self._malformed(cmd, "slot")
            return self.look(system, slot, bool(cmd.get("save", False)))
        return False

    def _malformed(self, cmd: Mapping[str, Any], field: str) -> bool:
        """Refuse a frame that named an action but not the field it needs.

        Debug, because a malformed frame is the browser's problem and not the
        host's — but *recorded*, because the page discards every response body,
        so this is the only evidence either end of the wire keeps."""
        log.debug(
            "perf console: command %r is missing or mistyped its %r field",
            cmd.get("action"),
            field,
        )
        return False


# The console page. Self-contained (inline CSS/JS, no CDN), phone-first: a sticky
# tempo bar with a locally-animated beat pulse and transport, a touch clip grid,
# an auto-generated effect rack, the current scene's tune knobs, the record of
# turning them, the look pads and a scene jump — one control for every action
# `PerfBridge.apply` dispatches, which `tests/test_perf_console.py` reads back
# out of this string and compares against that method's own source.
#
# It also shows the machine's screen, which is not a bridge action at all: one
# <img> against /api/screen/stream, because `multipart/x-mixed-replace` needs no
# decoder and no second socket, and a page with no build step cannot afford
# either. Off until asked — the host holds the machine's video stream up only
# while somebody is watching.
#
# State arrives over /perf/ws; commands go out as POSTs to /perf/*. The two
# exceptions are both /api routes that only a --serve host registers: the
# live-tune save-back (a *config write*, which needs a status code) and the
# screen. Both are handled as absent rather than assumed — this page is served
# by the control plane, which a plain CLI run has without any of /api. Kept
# dependency-free so it renders in any phone browser.
@functools.cache
def perf_page_html() -> str:
    """The console page, read once from the packaged ``perf_console.html``.

    A ~650-line HTML/CSS/JS document, so it lives in a real ``.html`` file
    rather than a Python string: as a string it got no syntax highlighting, no
    formatter, no linter, and could not be opened in a browser on its own while
    someone iterated on it. Nothing else changes — it is still one fixed,
    server-authored body with no caller content in it and no third-party
    resource to load, which is what lets :data:`_PAGE_HEADERS` be as strict as
    it is.

    Read through :mod:`importlib.resources` rather than ``__file__`` so any
    loader that imported the package answers, and cached because
    :func:`register_perf_routes` serves the same bytes on every request.
    ``read_text`` needs no real filesystem path (unlike
    :func:`c64cast.app.paths._package_dir`, whose callers hand paths to
    ``open()``), so this works from a zipped distribution too.

    Packaged by the ``control/*.html`` entry in ``[tool.setuptools.package-data]``
    — without it the wheel ships only ``.py`` files and the console 500s on a
    fresh install, which ``test_perf_console`` guards against by reading it."""
    from importlib.resources import files  # noqa: PLC0415  (lazy; import-time cost)

    return files("c64cast.control").joinpath("perf_console.html").read_text(encoding="utf-8")


#: Response headers for the console page.
#:
#: Hardening rather than a defense of its own, and ranked deliberately behind
#: the ``Origin`` check :func:`auth.same_origin` now applies: in the open mode
#: a hostile page could drive every control directly with no user interaction
#: at all, which is strictly easier than framing this page and tricking the
#: performer into tapping a pad; and in a token-gated deployment the
#: ``SameSite=Strict`` cookie is not sent into a third-party frame, so the
#: frame renders the login page instead. It costs the page nothing — a fixed,
#: server-authored body with no caller content in it and no third-party
#: resource to load — and ``unsafe-inline`` is what its own ``<style>`` and
#: ``<script>`` need. ``img-src`` has to allow ``self`` for the screen stream.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; "
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def register_perf_routes(app: Any, bridge: PerfBridge) -> None:
    """Register the performance-console routes on an existing FastAPI ``app``
    (the control plane's). Called from :func:`control_plane.build_app`. Imports
    FastAPI symbols locally (the app already required them) — real, non-stringized
    annotations so the WebSocket param injects correctly (see the module note)."""
    from fastapi import HTTPException, Request, Response, WebSocket

    feed = ConsoleFeed(
        "perf console",
        build_frame=lambda scope: with_role(bridge.state(), scope),
        dispatch=bridge.apply,
    )

    async def _command_body(request: Request) -> Mapping[str, Any]:
        """One console command's JSON body — same-origin, typed, and capped.

        This was the one POST in the package that skipped
        :func:`auth.read_body`: ``await request.json()`` accumulates every
        chunk of the body in memory before parsing it, with nothing bounding
        it, which is the hazard ``read_body``'s own docstring names ("a remote
        memory exhaustion on a 1-2 GB appliance, taking down a process that
        owns live hardware"). ``web_api._body`` has routed through it all
        along; this route now does too, at a cap far below
        ``auth.MAX_BODY_BYTES`` because a command is a few hundred bytes.

        The ``Content-Type`` requirement is not ceremony. ``Request.json()``
        never looks at it, so a cross-site ``<form enctype="text/plain">``
        whose field name and value sandwich the JSON is a CORS-simple POST that
        reached the dispatcher with no preflight to refuse."""
        if not same_origin(request.headers):
            raise HTTPException(403, "cross-origin request")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "a console command is application/json")
        try:
            raw = await read_body(request, max_bytes=MAX_COMMAND_BYTES)
        except BodyTooLarge as e:
            # The body cap goes to the log and a fixed string to the caller —
            # this route is reachable without a credential in the open mode.
            log.debug("perf console: %s", e)
            raise HTTPException(413, BODY_TOO_LARGE_ERROR) from e
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise HTTPException(400, "request body is not JSON") from e
        if not isinstance(parsed, Mapping):
            raise HTTPException(400, "request body must be a JSON object")
        return parsed

    @app.get("/perf")
    def perf_page() -> Response:
        return Response(content=perf_page_html(), media_type="text/html", headers=_PAGE_HEADERS)

    @app.get("/perf/state")
    def perf_state(request: Request) -> dict[str, Any]:
        # A sync `def` on purpose: FastAPI runs it in the threadpool, which is
        # where a frame build belongs (see `ConsoleFeed`).
        return with_role(bridge.state(), request.scope)

    @app.post("/perf/command")
    async def perf_command(request: Request) -> dict[str, Any]:
        body = await _command_body(request)
        # Off the loop for the same reason the feed's dispatch is: a
        # `mode.border` pick is a DMA write over TCP port 64.
        return {"ok": bool(await asyncio.to_thread(bridge.apply, body))}

    @app.websocket("/perf/ws")
    async def perf_ws(websocket: WebSocket) -> None:
        await feed.run(websocket)
