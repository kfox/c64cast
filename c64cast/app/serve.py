"""The session supervisor: one process, many sessions, over time.

The one-shot CLI builds a session, runs it, and exits — the process and the
session have the same lifetime, so "which session is this?" never comes up. A
long-lived host has to answer it: a browser asks for a show, the machine is
handed over, a different show replaces it, and the daemon outlives all of them.

:class:`SessionManager` is that state machine. It owns exactly one
:class:`~c64cast.app.session.Session` at a time and serialises every transition
behind one lock:

.. code-block:: text

                    start(req)                    build ok
      idle ──────────────────▶ starting ────────────────────▶ running
        ▲                          │                              │
        │                    build raised              stop() │ threads exited
        │                          ▼                              ▼
        └──── teardown done ─── stopping ◀──────────────────────────
                                   │
                     teardown done + build had failed
                                   ▼
                                 error ──start(req)──▶ starting

``error`` is not sticky: it is where a failed build parks its diagnostic, and
``start()`` from it is legal.

**Everything slow runs off the caller's thread.** ``build_session`` blocks for
many seconds (open the backend, reset, settle, install the char ROM, probe the
REU and the sampler), and teardown is not much cheaper, so ``start()`` and
``stop()` return as soon as the transition is claimed — an HTTP route answers
``202`` and the caller watches the state feed. What does *not* run off-thread is
validation: :func:`c64cast.app.session.validate_configs` is pure and
hardware-free, which is what lets a bad config be refused synchronously rather
than twenty seconds later from inside a worker.

**The build/teardown seam is injected**, which is the whole testability
argument: the state machine runs against fakes with no hardware, no sockets and
no sleeping. It is also why ``build`` is handed a ``publish`` callback rather
than simply returning a session — a build that fails *after* the stacks are up
(``start_services`` raising, say) has already taken the hardware, and the
alternative of making every build clean up after itself splits the "no hardware
is left held" guarantee across two places instead of keeping it here, on the
single path out of a generation.

**Two things a supervised session must never inherit from the CLI.** Signal
handlers, because ``signal.signal`` raises off the main thread; and
``Session.interactive``, which stays False so the live-tune ``input()`` prompt
(and the in-session control plane, whose port the host already holds) is
skipped. Both are handled by :func:`build_and_start`.

**Crash recovery.** A segfault in OpenCV or PyAV kills the process with the C64
mid-show and nobody left to reset it — the same exposure today's one-shot CLI
has. The marker file (:func:`c64cast.app.paths.run_marker_path`) is written on
``→ running`` and removed on a clean way down, so finding one at the next start
means the last run died: :func:`default_safe_state` opens a bare backend, resets
the machine, and closes it before anything else touches the hardware.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from c64cast._pollthread import PollThread
from c64cast.control.transport import atomic_write_text

from . import config as cfgmod
from . import paths
from .session import (
    Session,
    build_session,
    join_playlists,
    reload_all,
    start_playlists,
    start_services,
    teardown_session,
)

log = logging.getLogger("c64cast")


class SessionState(StrEnum):
    """Where the supervisor is. The values are the wire format — they go out
    over the state feed and into the UI verbatim."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


#: States a new session may be started from. Everything else is in transit.
STARTABLE = (SessionState.IDLE, SessionState.ERROR)


class SupervisorBusy(RuntimeError):
    """The supervisor can't honour the request from the state it is in.

    Carries the state it was in so a caller can say which — an HTTP route maps
    this to ``409``."""

    def __init__(self, state: SessionState, detail: str = ""):
        super().__init__(detail or f"supervisor is {state}")
        self.state = state


class _Cancelled(Exception):
    """Internal: a stop landed while the start worker was still coming up."""


@dataclass(frozen=True)
class StartRequest:
    """One "run this" request, already validated.

    Exactly what :func:`c64cast.app.session.build_session` needs, plus the
    config path for the run marker and the status feed. Built by the caller
    (which is what runs ``config.load_master`` + ``validate_configs``), so the
    supervisor never parses anything and never rejects a config — by the time a
    request reaches it, the answer is yes."""

    args: argparse.Namespace
    loaded: cfgmod.LoadResult
    cfgs: list[cfgmod.Config]
    config_path: str = ""


@dataclass(frozen=True)
class SessionStatus:
    """A snapshot of the supervisor, safe to serialise and to hand to a
    callback. Immutable on purpose: a state push that could be mutated after
    the fact by the next transition would be worse than useless."""

    state: SessionState
    generation: int
    config_path: str
    systems: tuple[str, ...]
    last_error: str | None
    #: Seconds a start would still have to wait for the hardware to settle.
    hardware_wait_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "generation": self.generation,
            "config_path": self.config_path,
            "systems": list(self.systems),
            "last_error": self.last_error,
            "hardware_wait_s": round(self.hardware_wait_s, 2),
        }


BuildFn = Callable[[StartRequest, int, Callable[[Session], None]], Session]
TeardownFn = Callable[[Session | None], None]
SafeStateFn = Callable[[StartRequest], None]
TransitionFn = Callable[[SessionStatus], None]


def build_and_start(
    req: StartRequest, generation: int, publish: Callable[[Session], None]
) -> Session:
    """The real build: hardware up, services up, playlists running.

    ``publish`` is called the moment the session object exists — from there on
    the supervisor owns teardown, so a failure in ``start_services`` or in
    thread start still releases the hardware."""
    sess = build_session(req.args, req.loaded, req.cfgs, interactive=False, generation=generation)
    publish(sess)
    start_services(sess)
    # Threads last: the reap poller treats a session with live threads as
    # running, so nothing may observe this session before it can actually run.
    sess.threads = start_playlists(sess.stacks)
    return sess


def teardown(sess: Session | None) -> None:
    """The real teardown. ``None`` is the build-failed-before-anything case."""
    if sess is None:
        return
    teardown_session(sess, save_live_tune=False)


def default_safe_state(req: StartRequest) -> None:
    """Put every machine the request names back to a known state after a run
    that died without tearing down.

    Deliberately the smallest possible bring-up — construct the backend, reset,
    close. Not ``_open_backend``: its probe, char-ROM install and provisioning
    all assume a machine that is about to be *used*, and a failure in any of
    them would abandon the reset, which is the one thing that has to happen."""
    from c64cast.hw.backend import make_backend

    for cfg, name in zip(req.cfgs, req.loaded.names, strict=False):
        try:
            api = make_backend(cfg)
        except Exception as e:
            log.error("[%s] safe-state reset skipped: %s", name, e)
            continue
        try:
            api.reset()
            log.info("[%s] reset after an unclean shutdown", name)
        except Exception:
            log.exception("[%s] safe-state reset failed", name)
        finally:
            try:
                api.close()
            except Exception:
                log.exception("[%s] safe-state close failed", name)


class SessionLogBuffer(logging.Handler):
    """A bounded in-memory tail of the ``c64cast`` logger, tagged by generation.

    A hardware failure's diagnostic is written to the log and nowhere else — in
    the CLI that is fine, the user is looking at the terminal. A daemon's user
    is looking at a browser, so the log has to be readable from there or the
    only answer the UI can give is "it didn't start"."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        #: Set by the supervisor, so a line can be attributed to the run it
        #: belongs to rather than to whatever is running when it is read.
        self.generation = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._records.append(
                {
                    "t": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    "message": record.getMessage(),
                    "generation": self.generation,
                }
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def tail(self, limit: int = 100, *, generation: int | None = None) -> list[dict[str, Any]]:
        """The most recent lines, oldest first, optionally for one generation."""
        rows = list(self._records)
        if generation is not None:
            rows = [r for r in rows if r["generation"] == generation]
        return rows[-limit:] if limit > 0 else rows

    def install(self, logger_name: str = "c64cast") -> None:
        logging.getLogger(logger_name).addHandler(self)

    def uninstall(self, logger_name: str = "c64cast") -> None:
        logging.getLogger(logger_name).removeHandler(self)


@dataclass
class _Workers:
    """The non-daemon threads a generation spawns, so `close` can wait on them.

    Non-daemon on purpose, and unlike every other background thread in the
    project: these are the threads that *tear the hardware down*. A daemon
    thread is killed at interpreter exit, which here means exiting mid-teardown
    with the machine still held — the wedge `daemon=False` on the playlist
    threads already exists to avoid."""

    threads: list[threading.Thread] = field(default_factory=list)

    def spawn(self, name: str, fn: Callable[[], None]) -> None:
        self.threads = [t for t in self.threads if t.is_alive()]
        t = threading.Thread(target=fn, name=name, daemon=False)
        self.threads.append(t)
        t.start()

    def join(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for t in list(self.threads):
            t.join(timeout=max(0.0, deadline - time.monotonic()))


class SessionManager:
    """Owns at most one session at a time; see the module docstring.

    Every callable is injected so the state machine is testable without
    hardware. ``clock`` is separate from the stdlib ``time`` this module also
    imports because :meth:`wait_for` reads a clock too — freezing one clock for
    both would freeze the test's own waiting alongside the cooldown it is
    trying to observe."""

    def __init__(
        self,
        *,
        build: BuildFn = build_and_start,
        teardown: TeardownFn = teardown,
        safe_state: SafeStateFn = default_safe_state,
        settle_s: float = 3.0,
        on_transition: TransitionFn | None = None,
        log_buffer: SessionLogBuffer | None = None,
        marker_path: Path | None = None,
        reap_period_s: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._build = build
        self._teardown = teardown
        self._safe_state = safe_state
        self._settle_s = settle_s
        self._on_transition = on_transition
        self._log_buffer = log_buffer
        self._marker_path = marker_path
        self._clock = clock

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._state = SessionState.IDLE
        self._generation = 0
        self._session: Session | None = None
        self._request: StartRequest | None = None
        self._last_error: str | None = None
        self._hardware_free_at = 0.0
        self._switching = False
        self._cancel = threading.Event()
        self._workers = _Workers()
        self._reaper = PollThread(
            self._reap_tick, period=reap_period_s, name="session-reap", run_first=False
        )

    # -- observation --------------------------------------------------------

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def session(self) -> Session | None:
        """The live session, or None. The reason this is exposed at all is the
        main-thread work a supervised session still needs — preview windows can
        only be pumped from there."""
        with self._lock:
            return self._session

    def status(self) -> SessionStatus:
        with self._lock:
            sess = self._session
            return SessionStatus(
                state=self._state,
                generation=self._generation,
                config_path=self._request.config_path if self._request is not None else "",
                systems=tuple(st.name for st in sess.stacks) if sess is not None else (),
                last_error=self._last_error,
                hardware_wait_s=max(0.0, self._hardware_free_at - self._clock()),
            )

    def wait_for(
        self,
        state: SessionState | Iterable[SessionState],
        timeout: float = 5.0,
        *,
        generation: int | None = None,
    ) -> bool:
        """Block until the supervisor is in one of ``state``. Returns whether it
        got there. Every test observes transitions through this rather than by
        sleeping, and the daemon's ``switch`` uses it to sequence stop → start.

        ``generation`` additionally requires that the state belongs to that run
        or a later one. Without it, "wait until running" is ambiguous across a
        switch — the show being replaced is running too."""
        wanted = (state,) if isinstance(state, SessionState) else tuple(state)
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._state not in wanted or (
                generation is not None and self._generation < generation
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # -- transitions --------------------------------------------------------

    def start(self, req: StartRequest) -> int:
        """Begin bringing a session up. Returns its generation; raises
        :class:`SupervisorBusy` unless the supervisor is idle or in error.

        Never an implicit stop: replacing a running show is :meth:`switch`, so
        the one place that has to get stop → settle → start right is the one
        place that does it."""
        with self._lock:
            if self._switching:
                raise SupervisorBusy(self._state, "a switch is already in progress")
            if self._state not in STARTABLE:
                raise SupervisorBusy(self._state, f"a session is already {self._state}")
            return self._begin_start_locked(req)

    def stop(self) -> bool:
        """Ask the current session to come down. Returns whether anything was
        asked — a stop from ``idle`` is a no-op, not an error, so a caller can
        stop unconditionally.

        A stop during ``starting`` cancels: the build is not interruptible
        (opening a backend blocks), so the flag is honoured at the next
        checkpoint and, failing that, immediately after the build lands."""
        with self._lock:
            if self._state == SessionState.STARTING:
                self._cancel.set()
                return True
            if self._state != SessionState.RUNNING:
                return False
            sess = self._session
            gen = self._generation
            self._transition_locked(SessionState.STOPPING)
            self._workers.spawn(f"session-stop-{gen}", lambda: self._run_stop(sess))
            return True

    def switch(self, req: StartRequest, *, timeout: float = 60.0) -> int:
        """Replace the running show: stop, wait for idle, honour the cooldown,
        start. Returns the generation the new session will have.

        One endpoint rather than making every caller sequence it, because the
        cooldown and the "don't start until the last teardown is *done*" rule
        are exactly what a caller gets wrong."""
        with self._lock:
            if self._switching:
                raise SupervisorBusy(self._state, "a switch is already in progress")
            if self._state not in (*STARTABLE, SessionState.RUNNING):
                raise SupervisorBusy(self._state, f"supervisor is {self._state}")
            self._switching = True
            pending = self._generation + 1
            self._workers.spawn(f"session-switch-{pending}", lambda: self._run_switch(req, timeout))
        return pending

    def reload(self) -> None:
        """Re-read the running session's configs and hand each playlist a fresh
        scene list — the daemon's reload button, and the same call the CLI's
        SIGHUP handler makes."""
        with self._lock:
            if self._state != SessionState.RUNNING or self._session is None:
                raise SupervisorBusy(self._state, "no session to reload")
            sess = self._session
        reload_all(sess)

    def close(self, *, timeout: float = 30.0) -> None:
        """Stop whatever is running and release the supervisor's own threads.
        Safe to call from any state, and safe to call twice."""
        self.stop()
        self.wait_for(STARTABLE, timeout=timeout)
        self._reaper.stop()
        self._workers.join(timeout=timeout)

    # -- internals ----------------------------------------------------------

    def _begin_start_locked(self, req: StartRequest) -> int:
        gen = self._generation + 1
        self._generation = gen
        self._request = req
        self._session = None
        self._last_error = None
        self._cancel.clear()
        if self._log_buffer is not None:
            self._log_buffer.generation = gen
        self._transition_locked(SessionState.STARTING)
        self._workers.spawn(f"session-start-{gen}", lambda: self._run_start(req, gen))
        return gen

    def _transition_locked(self, state: SessionState) -> None:
        """Move to ``state`` and wake every waiter. Callers hold the lock, so
        notifications go out in transition order — a listener that reordered
        ``running`` ahead of ``starting`` would be reporting a run that hadn't
        happened yet. The callback must not block for the same reason (the
        daemon's is a queue put)."""
        self._state = state
        log.debug("supervisor: generation %d is %s", self._generation, state)
        self._cond.notify_all()
        if self._on_transition is not None:
            snapshot = self.status()
            try:
                self._on_transition(snapshot)
            except Exception:
                log.exception("supervisor: transition callback failed")

    def _publish(self, sess: Session) -> None:
        with self._lock:
            self._session = sess

    def _run_start(self, req: StartRequest, gen: int) -> None:
        try:
            self._await_hardware()
            if self._recover_if_unclean(req):
                # A recovery just opened and closed the backend, which arms the
                # same settle window a teardown does — handing it straight to
                # the build is the socket-reuse case the cooldown exists for.
                self._arm_cooldown()
                self._await_hardware()
            sess = self._build(req, gen, self._publish)
        except _Cancelled:
            log.info("session %d: cancelled before it came up", gen)
            self._enter_stopping()
            self._settle(SessionState.IDLE, None)
            return
        except BaseException as e:
            log.exception("session %d failed to start", gen)
            self._enter_stopping()
            self._settle(SessionState.ERROR, f"{type(e).__name__}: {e}")
            return
        with self._lock:
            self._session = sess
            self._write_marker(req, gen)
            self._transition_locked(SessionState.RUNNING)
        self._reaper.start()
        # A stop that arrived mid-build has been waiting for exactly this: the
        # session is only stoppable once it is running.
        if self._cancel.is_set():
            self.stop()

    def _run_stop(self, sess: Session | None) -> None:
        try:
            if sess is not None:
                if any(t.is_alive() for t in sess.threads):
                    join_playlists(sess.threads, sess.stacks, sess.stop_event)
                else:
                    # Nothing to interrupt — the playlists ran out on their own.
                    sess.stop_event.set()
        except Exception:
            log.exception("session shutdown failed; tearing down anyway")
        self._settle(SessionState.IDLE, None)

    def _run_switch(self, req: StartRequest, timeout: float) -> None:
        try:
            self.stop()
            if not self.wait_for(STARTABLE, timeout=timeout):
                log.error("switch abandoned: the previous session is still %s", self.state)
                return
            with self._lock:
                # Cleared inside the lock so the start's own guard passes and
                # nothing else can claim the supervisor in between.
                self._switching = False
                self._begin_start_locked(req)
        finally:
            with self._lock:
                self._switching = False

    def _enter_stopping(self) -> None:
        with self._lock:
            self._transition_locked(SessionState.STOPPING)

    def _settle(self, final: SessionState, error: str | None) -> None:
        """Tear the generation down and land in ``final``.

        Teardown runs *outside* the lock: it takes seconds (every stack, in
        reverse, each closing hardware), and a status read that blocked on it
        would leave the UI unable to say why it was waiting. Nothing can start
        meanwhile — the state is ``stopping``, which isn't startable."""
        with self._lock:
            sess = self._session
        try:
            self._teardown(sess)
        except Exception:
            log.exception("session teardown failed")
        with self._lock:
            self._session = None
            self._clear_marker()
            self._last_error = error
            self._arm_cooldown()
            self._transition_locked(final)

    def _arm_cooldown(self) -> None:
        self._hardware_free_at = self._clock() + self._settle_s

    def _await_hardware(self) -> None:
        """Block until the settle window after the last teardown has passed.

        Two separate reasons, one timer: the U64's DMA service refuses new
        connections for a few seconds after one closes (docs/caveats.md), and
        macOS AVFoundation will not reopen a camera straight after
        ``WebcamSource.release()``."""
        while True:
            with self._lock:
                remaining = self._hardware_free_at - self._clock()
            if remaining <= 0:
                return
            if self._cancel.wait(min(remaining, 0.1)):
                raise _Cancelled()

    def _reap_tick(self) -> None:
        """Drive ``running → stopping`` when the playlists end by themselves.

        A non-looping show finishes on its own, and unlike the CLI — which is
        parked in a join and simply returns — the daemon has nothing watching
        for it. An empty thread list is not "finished": it is a session whose
        playlists were never started, and reaping it would tear down a run that
        hasn't happened."""
        with self._lock:
            if self._state != SessionState.RUNNING:
                return
            sess = self._session
            if sess is None or not sess.threads:
                return
            if any(t.is_alive() for t in sess.threads):
                return
            log.info("session %d: every playlist finished", self._generation)
            gen = self._generation
            self._transition_locked(SessionState.STOPPING)
            self._workers.spawn(f"session-stop-{gen}", lambda: self._run_stop(sess))

    # -- the run marker -----------------------------------------------------

    def _marker(self) -> Path:
        return self._marker_path if self._marker_path is not None else paths.run_marker_path()

    def _write_marker(self, req: StartRequest, gen: int) -> None:
        payload = {
            "pid": os.getpid(),
            "generation": gen,
            "config_path": req.config_path,
            "started_at": time.time(),
        }
        try:
            atomic_write_text(self._marker(), json.dumps(payload, indent=2) + "\n")
        except OSError:
            # A missing marker costs a safe-state reset on the next start, not
            # correctness — never worth failing a show that is otherwise up.
            log.exception("could not write the run marker")

    def _clear_marker(self) -> None:
        try:
            self._marker().unlink(missing_ok=True)
        except OSError:
            log.exception("could not remove the run marker")

    def _recover_if_unclean(self, req: StartRequest) -> bool:
        """Reset the machine if the last run died without tearing down.
        Returns whether it did, since a recovery touches the hardware and the
        caller has to settle again before the build reopens it."""
        marker = self._marker()
        if not marker.exists():
            return False
        log.warning(
            "found a run marker at %s — the previous session did not shut down "
            "cleanly; resetting the machine before starting",
            marker,
        )
        try:
            self._safe_state(req)
        except Exception:
            log.exception("safe-state recovery failed; starting anyway")
        self._clear_marker()
        return True


def request_from_configs(
    args: argparse.Namespace,
    loaded: cfgmod.LoadResult,
    cfgs: Sequence[cfgmod.Config],
    *,
    config_path: str = "",
) -> StartRequest:
    """Build a :class:`StartRequest` from an already-validated load. A one-liner
    that exists so callers don't have to remember the field order."""
    return StartRequest(args=args, loaded=loaded, cfgs=list(cfgs), config_path=config_path)
