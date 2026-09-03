"""The session supervisor: one process, many sessions, over time.

The one-shot CLI builds a session, runs it, and exits — the process and the
session have the same lifetime, so "which session is this?" never comes up. A
long-lived host has to answer it: a browser asks for a show, the machine is
handed over, a different show replaces it, and the daemon outlives all of them.

:class:`SessionManager` is that state machine. It owns exactly one
:class:`~c64cast.app.session.Session` at a time and serializes every transition
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
import contextlib
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from c64cast._pollthread import PollThread
from c64cast._redact import redact_secrets
from c64cast.control.auth import ViewerCredential
from c64cast.control.transport import atomic_write_text
from c64cast.video.preview import PreviewWindow

from . import config as cfgmod
from . import config_store, console_library, media_store, paths
from .session import (
    Session,
    build_session,
    join_bounded,
    join_playlists,
    make_stop_signal_handler,
    reload_all,
    reload_registries,
    start_playlists,
    start_services,
    teardown_session,
    validate_configs,
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
    """The supervisor can't honor the request from the state it is in.

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
    """A snapshot of the supervisor, safe to serialize and to hand to a
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

    if len(req.cfgs) != len(req.loaded.names):
        # `strict=False` below truncates to the shorter of the two, which in
        # the one function whose whole job is guaranteeing a reset would drop
        # a machine with nothing said about it. The loop still runs — a reset
        # that can happen must — but the mismatch is a bug upstream and has to
        # be visible.
        log.error(
            "safe-state reset: %d configs but %d system names; some machines may be missed",
            len(req.cfgs),
            len(req.loaded.names),
        )

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
    only answer the UI can give is "it didn't start".

    **The lock is ``logging.Handler``'s own, and every reader takes it.**
    ``emit`` is called with it already held (``Handler.handle`` acquires it),
    which is also what makes ``seq += 1`` safe — a test calling ``emit``
    directly bypasses that. The readers run on other threads entirely (the
    state feed's push loop, a threadpool route), and a deque being appended to
    cannot be iterated at the Python level: an append — or, at ``maxlen``, the
    eviction that comes with it — raises ``RuntimeError: deque mutated during
    iteration`` in the reader. So both readers go through
    :meth:`_snapshot` rather than one of them being written safely and the
    other not."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        #: Set by the supervisor, so a line can be attributed to the run it
        #: belongs to rather than to whatever is running when it is read.
        self.generation = 0
        #: Monotonic line counter. A follower asks for what it hasn't seen by
        #: number, which is what lets the state feed carry the log without
        #: re-sending the whole tail three times a second.
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seq += 1
            self._records.append(
                {
                    "seq": self.seq,
                    "t": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    # Redacted on the way *in*, because this buffer is served to
                    # every client on the state feed — including a read-only
                    # viewer, who must not be handed the token that would let it
                    # stop the show. See `c64cast._redact`.
                    "message": redact_secrets(record.getMessage()),
                    "generation": self.generation,
                }
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def _snapshot(self) -> list[dict[str, Any]]:
        """A copy of the retained records, taken under the handler lock.

        ``Handler.acquire``/``release`` rather than ``with self.lock``: the
        attribute is optional on the base class, and these two methods are
        what handle that. Deadlock-free because nothing in here logs, and the
        lock is reentrant anyway."""
        self.acquire()
        try:
            return list(self._records)
        finally:
            self.release()

    def tail(self, limit: int = 100, *, generation: int | None = None) -> list[dict[str, Any]]:
        """The most recent ``limit`` lines, oldest first, optionally for one
        generation. A non-positive ``limit`` asks for nothing and gets nothing
        — ``rows[-0:]`` is the whole list, which would make ``limit=0`` mean
        "every retained line" to anything that ever forwards a client's own
        number."""
        if limit <= 0:
            return []
        rows = self._snapshot()
        if generation is not None:
            rows = [r for r in rows if r["generation"] == generation]
        return rows[-limit:]

    def since(self, seq: int) -> list[dict[str, Any]]:
        """Lines newer than ``seq``, oldest first. A follower that fell far
        enough behind for its lines to age out of the deque silently skips
        them — the alternative is a console that can never catch up."""
        return [r for r in self._snapshot() if r["seq"] > seq]

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
            remaining = max(0.0, deadline - time.monotonic())
            join_bounded(t, remaining)


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
        launch_config_path: str = "",
    ) -> None:
        self._build = build
        self._teardown = teardown
        self._safe_state = safe_state
        self._settle_s = settle_s
        self._on_transition = on_transition
        self._log_buffer = log_buffer
        self._marker_path = marker_path
        self._clock = clock
        # What `status()` answers before the console has started anything —
        # the config this host was launched with, so the browser has
        # something to preselect and show as "the running config" even at
        # idle. There is no other "host default" concept left: once a start
        # names a ref, `_request.config_path` is that ref instead.
        self._launch_config_path = launch_config_path

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._state = SessionState.IDLE
        self._generation = 0
        self._session: Session | None = None
        self._request: StartRequest | None = None
        self._last_error: str | None = None
        self._hardware_free_at = 0.0
        self._switching = False
        #: Set by `stop()` (and by `close()`, through it) while a switch is in
        #: flight: the switch worker checks it under the lock immediately
        #: before it starts the replacement show, so a stop that lands in the
        #: idle gap between the two halves of a switch is honored instead of
        #: being answered `202` and then overridden.
        self._abort_switch = False
        #: Terminal. `close()` sets it, and nothing may take the hardware
        #: afterward — otherwise a Ctrl+C during a console switch left a
        #: brand-new session running with nobody left to stop it.
        self._closing = False
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
                config_path=(
                    self._request.config_path
                    if self._request is not None
                    else self._launch_config_path
                ),
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
            self._refuse_if_closing()
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
        (opening a backend blocks), so the flag is honored at the next
        checkpoint and, failing that, immediately after the build lands.

        A stop during a :meth:`switch` also cancels the *pending* start. It
        consulted only ``_state`` before, which meant an operator's stop was
        silently discarded for the whole duration of a switch — refused as
        "not running" during the teardown, and dropped in the idle gap before
        the replacement was claimed — and the switch then took the hardware
        anyway. "Press stop, get 202, machine starts a show" is not a contract
        this surface gets to have."""
        with self._lock:
            aborting = self._switching
            if aborting:
                self._abort_switch = True
            return self._stop_locked() or aborting

    def switch(self, req: StartRequest, *, timeout: float = 60.0) -> int:
        """Replace the running show: stop, wait for idle, honor the cooldown,
        start. Returns the generation the new session will have.

        One endpoint rather than making every caller sequence it, because the
        cooldown and the "don't start until the last teardown is *done*" rule
        are exactly what a caller gets wrong.

        The returned generation is what the switch *intends*; it is not a
        promise. A previous session that will not come down, a stop that lands
        mid-switch, and :meth:`close` all abandon the pending start — each of
        them leaving the reason in ``last_error`` and re-notifying, so a caller
        watching the state feed learns why the generation it was given never
        arrived."""
        with self._lock:
            self._refuse_if_closing()
            if self._switching:
                raise SupervisorBusy(self._state, "a switch is already in progress")
            if self._state not in (*STARTABLE, SessionState.RUNNING):
                raise SupervisorBusy(self._state, f"supervisor is {self._state}")
            self._switching = True
            self._abort_switch = False
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
        Safe to call from any state, and safe to call twice.

        **Terminal**: :meth:`start` and :meth:`switch` refuse afterward, and a
        switch already in flight abandons its pending start. Without that,
        ``close()`` woke the parked switch worker on the very transition it was
        waiting for — the worker cleared ``_switching``, claimed a new
        generation, and ``_workers.join`` then waited for it to finish
        *building*, so a Ctrl+C during a console switch returned from
        ``close()`` with a session running, the marker written and the machine
        held, straight into ``cli.ensure_exit``'s force-exit."""
        with self._lock:
            self._closing = True
            busy = self._state not in STARTABLE or self._switching
        if busy:
            # Only when there is something to wait on: `run_daemon` calls this
            # on every exit path, including the ones where no session ever
            # existed, and a host that died resolving its token used to sign
            # off with a claim that it was waiting a minute on a teardown.
            log.info("waiting up to %.0fs for the session to tear down", timeout * 2)
        self.stop()
        self.wait_for(STARTABLE, timeout=timeout)
        self._reaper.stop()
        self._workers.join(timeout=timeout)
        if self.state not in STARTABLE:
            # A start that squeezed through before `_closing` was visible, or
            # a teardown that outran its deadline. This is the last chance to
            # release the hardware, so ask once more.
            log.warning("the session is still %s after close(); stopping it again", self.state)
            self.stop()
            self.wait_for(STARTABLE, timeout=timeout)
            self._workers.join(timeout=timeout)

    # -- internals ----------------------------------------------------------

    def _refuse_if_closing(self) -> None:
        if self._closing:
            raise SupervisorBusy(self._state, "the supervisor is shutting down")

    def _begin_start_locked(self, req: StartRequest) -> int:
        previous = self._generation
        gen = previous + 1
        self._generation = gen
        self._request = req
        self._session = None
        self._last_error = None
        self._cancel.clear()
        if self._log_buffer is not None:
            self._log_buffer.generation = gen
        self._transition_locked(SessionState.STARTING)
        try:
            self._workers.spawn(f"session-start-{gen}", lambda: self._run_start(req, gen))
        except Exception as e:
            # Nothing else can leave `starting`: it isn't STARTABLE, `stop()`
            # from it only arms the cancel flag, and the reaper ignores it — so
            # a `Thread.start()` that raised (resource exhaustion) wedged the
            # host until the process restarted. Roll back to a startable state
            # and still tell the caller.
            self._generation = previous
            if self._log_buffer is not None:
                self._log_buffer.generation = previous
            self._record_error_locked(f"{type(e).__name__}: {e}")
            self._transition_locked(SessionState.ERROR)
            raise
        return gen

    def _stop_locked(self) -> bool:
        """The body of :meth:`stop`, minus the switch-abort flag — the switch
        worker's own stop must not abort the switch it is performing."""
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

    def _record_error_locked(self, error: str | None) -> None:
        """Set ``last_error``, redacted on the way *in*.

        Exactly why :meth:`SessionLogBuffer.emit` redacts: the status snapshot
        goes out on the state feed and from ``GET /api/session`` to every role,
        read-only viewers included, and a build failure's exception text can
        quote a connection URL with its link knobs or a value some library
        echoed back. The sibling ``log`` key on that same frame was already
        redacted; this was the one field on it that wasn't. Done here rather
        than in :meth:`SessionStatus.as_dict` so the next field added to the
        snapshot doesn't get it wrong the same way."""
        self._last_error = redact_secrets(error) if error else error

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
            # `BaseException`, not `Exception`, and load-bearing: a build that
            # raised `SystemExit` or `GeneratorExit` has already taken the
            # hardware, and `_settle` is the only thing that releases it —
            # narrowing this reopens the "no hardware is left held" guarantee
            # this module is built around. Not re-raised, either: this is a
            # worker thread, where `threading.excepthook` discards `SystemExit`
            # outright, so the only useful thing left is to say which it was
            # rather than letting a deliberate exit read as a dead machine.
            if isinstance(e, Exception):
                log.exception("session %d failed to start", gen)
            else:
                log.exception("session %d: the build was interrupted by %s", gen, type(e).__name__)
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
        error: str | None = None
        try:
            if sess is not None:
                if any(t.is_alive() for t in sess.threads):
                    join_playlists(sess.threads, sess.stacks, sess.stop_event)
                else:
                    # Nothing to interrupt — the playlists ran out on their own.
                    sess.stop_event.set()
        except Exception as e:
            log.exception("session shutdown failed; tearing down anyway")
            # Carried into `last_error` for the same reason a failed *build* is:
            # a teardown that raised is the one thing an operator most needs to
            # know about, and settling with `None` left the console reporting a
            # clean `idle`.
            error = f"session shutdown failed: {type(e).__name__}: {e}"
        self._settle(SessionState.IDLE, error)

    def _run_switch(self, req: StartRequest, timeout: float) -> None:
        try:
            with self._lock:
                self._stop_locked()
            if not self.wait_for(STARTABLE, timeout=timeout):
                self._abandon_switch(f"the previous session is still {self.state}")
                return
            with self._lock:
                if self._abort_switch or self._closing:
                    self._abandon_switch_locked("a stop landed while it was in flight")
                    return
                # Cleared inside the lock so the start's own guard passes and
                # nothing else can claim the supervisor in between.
                self._switching = False
                self._begin_start_locked(req)
        finally:
            with self._lock:
                self._switching = False
                self._abort_switch = False

    def _abandon_switch(self, reason: str) -> None:
        with self._lock:
            self._abandon_switch_locked(reason)

    def _abandon_switch_locked(self, reason: str) -> None:
        """Give up on the pending start, loudly.

        The generation :meth:`switch` handed its caller is never going to
        arrive, so the reason goes into ``last_error`` and the current state is
        re-notified: a browser holding a ``202`` and waiting on that generation
        would otherwise see nothing at all change, with ``last_error: null``,
        and the only diagnostic would be on the host's stderr."""
        log.error("switch abandoned: %s", reason)
        self._record_error_locked(f"switch abandoned: {reason}")
        self._transition_locked(self._state)

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
            self._record_error_locked(error)
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
            # Named apart from `stop()`'s worker on purpose: `join_bounded`
            # identifies a straggler by thread name, and "session-stop-7 did
            # not finish" cannot tell an operator-requested stop (a wedged
            # playlist) from a self-ending show being reaped (a hung teardown).
            self._workers.spawn(f"session-reap-stop-{gen}", lambda: self._run_stop(sess))

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


# ---------------------------------------------------------------------------
# The host: one server, many sessions
# ---------------------------------------------------------------------------

#: Produces the per-system configs to run — the CLI's own resolver, handed in
#: rather than imported, because `cli` imports this module to dispatch
#: ``--serve``. Called on every start, so an edit to the TOML lands on the next
#: one without restarting the host.
#:
#: The argument is the config path to run, or ``None`` for the one the host was
#: launched with. It hands back the ``Namespace`` it resolved *against* as well
#: as the configs: a start on a browser-chosen path can't reuse the launch
#: namespace (its ``config`` names a different file, and ``build_session`` reads
#: it), and mutating the shared one instead would leave the next default start
#: pointing at whatever was launched last.
ConfigLoader = Callable[
    [str | None], tuple[argparse.Namespace, cfgmod.LoadResult, list[cfgmod.Config]]
]


def make_request_factory(
    load: ConfigLoader, *, config_path: str = ""
) -> Callable[[str | None], StartRequest]:
    """Wrap a config loader into the "give me something to run" callable the
    API routes hold.

    Validation happens here, on the request thread, so a config that can't run
    is refused before any transition is claimed — see the ``422`` note in
    :mod:`c64cast.control.web_api`.

    **Validation is not confinement, and this is not where confinement
    happens.** The ``path`` is handed straight to the injected
    :data:`ConfigLoader`, which by design opens any path it is given (it is
    the CLI's own resolver). Keeping a browser-supplied ref inside
    ``[web].config_roots`` is :class:`~c64cast.app.config_store.ConfigStore`'s
    job, and every caller resolves through it *before* calling this — a route
    that forwarded a raw client string would turn this into an arbitrary-file
    config load, and a loaded config names media paths and URLs a session
    opens."""

    def factory(path: str | None = None) -> StartRequest:
        args, loaded, cfgs = load(path)
        validate_configs(loaded, cfgs)
        return request_from_configs(args, loaded, cfgs, config_path=path or config_path)

    return factory


class TokenSource(StrEnum):
    """Which of the four sources answered for the full token.

    The values are prose because they are logged verbatim: an operator looking
    at a host that is answering with a credential they don't recognize needs to
    know *which* file or variable it came from."""

    ENV = "the C64CAST_WEB_TOKEN environment variable"
    CONFIG = "[web] token"
    FILE = "[web] token_file"
    GENERATED = "the generated token under the data dir"


@dataclass(frozen=True)
class HostCredentials:
    """What the host authenticates with, and where its full token came from."""

    token: str
    viewer: ViewerCredential
    source: TokenSource

    @property
    def token_settable(self) -> bool:
        """Whether the appliance setup form may write a replacement token.

        Provenance rather than a second reading of the same three sources.
        Only a token :func:`resolve_tokens` *generated* can be replaced: it
        consults the file the form writes last of all four, so a configured
        token would silently outrank a replacement and lock the admin out on
        the very next restart. ``run_daemon`` used to re-derive that inline as
        ``not (os.environ.get(...) or cfg.token or cfg.token_file)``, which is
        the same knowledge encoded twice with a security-relevant flag as the
        casualty of any drift."""
        return self.source is TokenSource.GENERATED


def resolve_tokens(cfg: cfgmod.WebCfg) -> HostCredentials:
    """Settle the host's credentials.

    Precedence for the full token is env → config → ``token_file`` →
    generated-and-persisted, and the last step is why the token is never
    empty. ``[control]`` may run unauthenticated because that is what it has
    always done and breaking those runs isn't a trade a security feature gets
    to make; this surface has no history to preserve and starts hardware, so
    "no token" is not one of its states.

    A read-only token equal to the full one is refused here rather than
    further in: :func:`c64cast.control.auth.match_role` compares the full
    token first, so the same secret pasted into both fields — or both fed from
    one secret-manager entry — silently granted every holder of the
    "read-only" link start, stop, config writes and media upload.
    :func:`~c64cast.control.auth.install_auth` does refuse it, but with a
    ``ValueError`` from inside app construction; refusing it here is what
    turns that into a named reason and a nonzero exit."""
    source = TokenSource.ENV
    token = os.environ.get("C64CAST_WEB_TOKEN")
    if not token:
        source = TokenSource.CONFIG
        token = cfg.token
    if not token and cfg.token_file:
        source = TokenSource.FILE
        path = Path(paths.expand_user(cfg.token_file))
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(f"could not read [web] token_file {path}: {e}") from e
        if not token:
            raise RuntimeError(f"[web] token_file {path} is empty")
    if not token:
        source = TokenSource.GENERATED
        token = _generated_token()
    viewer = os.environ.get("C64CAST_WEB_VIEWER_TOKEN") or cfg.viewer_token
    if not viewer:
        # A previously-issued one, if there is one. Not minted here: see
        # `ViewerCredential` and `paths.web_viewer_token_path`.
        with contextlib.suppress(OSError):
            viewer = paths.web_viewer_token_path().read_text(encoding="utf-8").strip()
    if viewer and viewer == token:
        raise RuntimeError(
            "[web] viewer_token is the same value as the full token — every holder of "
            "the read-only link would have full control of the machine"
        )
    return HostCredentials(
        token=token,
        viewer=ViewerCredential(viewer, store=_persist_viewer_token),
        source=source,
    )


def _persist_viewer_token(token: str) -> None:
    """Keep a minted read-only token, ``0600``, beside the full one — so a link
    handed to a guest still opens after the next restart."""
    path = paths.web_viewer_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        log.warning("could not restrict permissions on %s", path)


def _generated_token() -> str:
    """Read (or mint) the persisted token under the data dir, ``0600``.

    Persisted rather than regenerated per run so a phone that has the URL
    bookmarked keeps working across restarts."""
    import secrets

    path = paths.web_token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        log.warning("could not restrict permissions on %s", path)
    return token


def build_daemon_app(
    manager: SessionManager,
    request_factory: Callable[[str | None], StartRequest],
    *,
    token: str,
    viewer_token: str | ViewerCredential = "",
    log_buffer: SessionLogBuffer | None = None,
    store: config_store.ConfigStore | None = None,
    library: console_library.ConsoleLibrary,
    media: media_store.MediaStore,
    screen_fps: float = 10.0,
    setup_pending: bool = False,
    token_settable: bool = True,
    on_setup_complete: Callable[[], None] | None = None,
) -> Any:
    """The host's FastAPI app: the control plane over the *current* session,
    plus the ``/api/*`` routes that create and destroy sessions.

    One app, built once per :func:`run_daemon` loop iteration — never rebuilt
    while it is serving, since tearing it down per session would drop the
    listening socket and every connected console on each show change, which is
    exactly what the registry providers exist to avoid. The loop *does* rebuild
    this whole app, once, when setup completes (``on_setup_complete`` fires):
    a token chosen during setup has to reach ``TokenAuthMiddleware``'s
    constructor, which takes a plain string, so there is no cheaper way to
    pick it up than building the app this function returns a second time.

    ``setup_pending`` — see :mod:`c64cast.control.setup_gate` — adds the setup
    form's API and, **last**, the gate that hides everything else behind it;
    both are omitted entirely once setup has completed, rather than switched
    off, so there is nothing left here for a stray request to reach.
    ``token_settable`` rides along to that API: only the credential resolution
    knows whether ``token`` was generated (and so can be replaced by the form)
    or named by configuration (and so cannot) — see
    :attr:`HostCredentials.token_settable`.

    ``library`` and ``media`` are required, because both constructors resolve
    into the data dir and write there: a ``None`` default meant a caller who
    forgot one got a component quietly writing under
    ``~/.local/share/c64cast`` instead of a ``TypeError``. :func:`run_daemon`
    builds them where it builds ``store``, which is the one place that owns
    the data dir."""
    from c64cast.control.control_plane import build_app_for_registry
    from c64cast.control.setup_api import register_setup_routes
    from c64cast.control.setup_gate import SETUP_PAGE_PATH, SETUP_PATH, install_setup_gate
    from c64cast.control.web_api import register_web_routes
    from c64cast.control.web_static import mount_web_app, shell_paths

    def playlists() -> dict[str, Any]:
        sess = manager.session
        if sess is None:
            return {}
        return {st.name: st.playlist for st in sess.stacks}

    def _registries(index: int) -> dict[str, Any]:
        sess = manager.session
        if sess is None:
            return {}
        return reload_registries(sess)[index]

    app = build_app_for_registry(
        playlists,
        lambda: _registries(0),
        lambda: _registries(1),
        token=token,
        viewer_token=viewer_token,
        # The setup form has to answer with no token at all — nothing has
        # generated one the admin has seen yet — so it, the shell that draws
        # it, and the shell's own address for it need the same allowlist
        # `auth.PUBLIC_PATHS` gives the login exchange. This is
        # `TokenAuthMiddleware`'s exemption, not `SetupGateMiddleware`'s: the
        # gate below only decides what's blocked *while pending*, and by
        # itself would still hand these paths on into a token check they
        # can't pass.
        public_paths=((SETUP_PATH, SETUP_PAGE_PATH, *shell_paths()) if setup_pending else ()),
    )
    register_web_routes(
        app,
        manager=manager,
        request_factory=request_factory,
        playlists=playlists,
        log_buffer=log_buffer,
        store=store if store is not None else config_store.ConfigStore(),
        library=library,
        media=media,
        # The very object the gate reads, so a token minted from the console is
        # accepted by the next request rather than by the next restart.
        viewer=viewer_token if isinstance(viewer_token, ViewerCredential) else None,
        screen_fps=screen_fps,
    )
    if setup_pending:
        if on_setup_complete is None:
            raise ValueError("setup_pending needs an on_setup_complete callback")
        register_setup_routes(
            app,
            token=token,
            token_settable=token_settable,
            on_complete=on_setup_complete,
        )
    # mount_web_app is last among the *route* registrations: its fallback is a
    # catch-all, so anything registered after it would be unreachable. The
    # setup gate goes even later than that — it reads the app's complete route
    # table (`setup_gate.install_setup_gate` -> `web_static.owned_segments`) to
    # decide what to block, so it has to see everything above first.
    mount_web_app(app)
    if setup_pending:
        install_setup_gate(app)
    return app


def _open_previews(sess: Session | None) -> list[PreviewWindow]:
    if sess is None:
        return []
    windows = [st.preview_window for st in sess.stacks if st.preview_window is not None]
    for w in windows:
        try:
            w.open()
        except Exception:
            log.exception("preview window failed to open")
    return [w for w in windows if w.is_open]


def _close_previews(windows: list[PreviewWindow]) -> None:
    for w in windows:
        try:
            w.close()
        except Exception:
            log.exception("preview window failed to close")


def pump_forever(
    manager: SessionManager,
    shutdown: threading.Event,
    poll_s: float = 0.05,
    *,
    restart: threading.Event | None = None,
) -> None:
    """Park the main thread for the life of one :func:`run_daemon` loop
    iteration, servicing whatever preview windows the current session owns.

    HighGUI may only create and service a window on the process's main thread,
    and under ``--serve`` that thread is here rather than in a join. Opening
    and closing windows repeatedly across sessions in one process is the
    least-exercised corner of this design — ``[preview]`` under a host is
    documented as "works from a terminal", not a supported deployment — so
    every call into a window is contained rather than allowed to take the host
    down with it.

    ``restart`` — set once the appliance setup form completes — ends this call
    exactly like ``shutdown`` does, but ``run_daemon`` tells the two apart on
    return: a shutdown ends the process, a restart rebuilds the app and calls
    back in. Callers outside ``run_daemon`` never pass it."""

    def _stopping() -> bool:
        return shutdown.is_set() or (restart is not None and restart.is_set())

    opened: list[PreviewWindow] = []
    generation = -1
    try:
        while not _stopping():
            sess = manager.session
            gen = sess.generation if sess is not None else 0
            if gen != generation:
                _close_previews(opened)
                opened = _open_previews(sess)
                generation = gen
            if not opened:
                shutdown.wait(poll_s)
                continue
            for w in opened:
                try:
                    w.pump()
                except Exception:
                    log.exception("preview window failed to draw")
            # The user closing the window doesn't stop the show; it just stops
            # this loop from drawing one (the CLI's pump does the same).
            if not any(w.is_open for w in opened):
                opened = []
    finally:
        _close_previews(opened)


@dataclass(frozen=True)
class _Host:
    """Everything :func:`run_daemon` builds once and every serve cycle reuses.

    Bundled so :func:`_serve_once` takes the cycle's own inputs rather than
    seven loop-invariant ones. Nothing in here changes across a setup restart:
    only the app, its listener and its mDNS record are per-cycle."""

    manager: SessionManager
    factory: Callable[[str | None], StartRequest]
    log_buffer: SessionLogBuffer
    store: config_store.ConfigStore
    media: media_store.MediaStore
    library: console_library.ConsoleLibrary
    shutdown: threading.Event


class _Cycle(StrEnum):
    """How one serve cycle ended, which is the only thing left for
    :func:`run_daemon` to act on."""

    SHUTDOWN = "shutdown"
    RESTART = "restart"
    FAILED = "failed"


#: Machine-settings tables that carry a connection target —
#: :func:`c64cast.app.connect.apply_to_config` writes into exactly these three,
#: so any of them being present means somebody already told this host what
#: hardware to talk to.
_CONNECTION_TABLES = ("hardware", "ultimate64", "teensyrom")


def _install_signal_handlers(manager: SessionManager, shutdown: threading.Event) -> None:
    """SIGTERM/SIGINT end the host; SIGHUP reloads the running session.

    The ``--serve`` process is a foreground process too, so it gets the same
    three-strike factory ``cli._run_session`` uses: the first signal sets
    ``shutdown`` so the pump exits and the session is torn down, the second
    restores ``SIG_DFL`` for that signal so a service manager's repeated
    SIGTERM isn't caught forever, and the third actually kills."""
    on_stop = make_stop_signal_handler(shutdown.set, verb="shutting down the host")

    def _on_sighup(_signum: int, _frame: Any) -> None:
        log.info("SIGHUP received")
        try:
            manager.reload()
        except SupervisorBusy as e:
            log.warning("reload ignored: %s", e)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        signal.signal(sighup, _on_sighup)


def _looks_provisioned() -> bool:
    """Whether machine settings already name a connection target.

    Read from the *config* dir, which is what makes it corroborating evidence
    for :func:`_setup_pending`: the setup marker lives under the *data* dir,
    and the whole failure mode is one of those two outliving the other. An
    unreadable settings file counts as provisioned — the safe answer when the
    question being decided is "may this host serve an unauthenticated form?"."""
    try:
        data = cfgmod.load_machine_settings()
    except Exception:
        log.exception("could not read machine settings; assuming this host is provisioned")
        return True
    return any(isinstance(data.get(table), dict) and data[table] for table in _CONNECTION_TABLES)


def _setup_pending(web_cfg: cfgmod.WebCfg) -> bool:
    """Whether to open the appliance's unauthenticated setup window.

    The absence of ``setup.json`` used to be the only evidence, and it cannot
    tell "this is a first boot" from "this host lost its data dir": a container
    layer with no volume, a tmpfs, or a cleanup sweep then reopened
    ``/api/setup``, ``/setup`` and the whole shell to the segment on a fully
    configured appliance — where whoever won the race could repoint the
    connection at a host they control and, on a host relying on the generated
    token, write a replacement admin credential. So a missing marker on a host
    that *looks* provisioned is treated as the loss it is and the window stays
    shut; ``c64cast --reset-setup`` leaves an explicit reopen marker
    (:func:`c64cast.app.paths.setup_reopen_path`) precisely so an admin who
    already has shell access can still ask for it."""
    if not web_cfg.setup_wizard or paths.setup_state_path().is_file():
        return False
    if paths.setup_reopen_path().is_file():
        log.warning("web console: --reset-setup asked for the setup window; opening it")
        return True
    if _looks_provisioned():
        log.warning(
            "web console: %s is missing but machine settings already name a connection "
            "target, so the unauthenticated setup window stays shut. Run "
            "`c64cast --reset-setup` if you did mean to configure this host again.",
            paths.setup_state_path(),
        )
        return False
    return True


def _clear_setup_reopen() -> None:
    """Spend the ``--reset-setup`` marker once setup has completed, so a later
    loss of ``setup.json`` falls back to the provisioning evidence rather than
    to a reopen nobody asked for twice."""
    with contextlib.suppress(OSError):
        paths.setup_reopen_path().unlink(missing_ok=True)


def _log_setup_url(web_cfg: cfgmod.WebCfg) -> None:
    """Announce the setup window — at ``warning``, because what it announces is
    a host answering with no authentication at all."""
    from c64cast.control.setup_gate import SETUP_PAGE_PATH

    log.warning(
        "web console: setup required — this host is answering http://%s:%d%s with no "
        "authentication until it is configured",
        web_cfg.host,
        web_cfg.port,
        SETUP_PAGE_PATH,
    )


def _log_console_urls(
    web_cfg: cfgmod.WebCfg, creds: HostCredentials, store: config_store.ConfigStore
) -> None:
    """The startup banner for a configured host.

    Straight to the console when there is one, and to the zero-dependency
    ``/perf`` page when the bundle was never built — the printed URL is the
    only entry point a phone gets, so it has to land somewhere useful. The
    token's provenance is named because a planted ``web_token`` under a
    group-writable data dir is adopted silently otherwise."""
    from c64cast.control.web_static import landing_path

    log.info(
        "web console: open http://%s:%d/api/login?token=%s&next=%s",
        web_cfg.host,
        web_cfg.port,
        creds.token,
        landing_path(),
    )
    log.info("web console: the full token comes from %s", creds.source)
    if creds.viewer:
        log.info("web console: a read-only token is configured as well")
    else:
        log.info("web console: ask it for a read-only link when you want to share the screen")
    log.info(
        "web console: editable config roots: %s",
        ", ".join(f"{r.label} = {r.path}" for r in store.roots) or "none",
    )


def _serve_once(host: _Host, web_cfg: cfgmod.WebCfg) -> _Cycle:
    """One build-and-pump cycle: credentials, app, listener, mDNS, pump.

    **Shutdown ordering is load-bearing and lives here.** On the way out under
    ``shutdown`` the session comes down *before* the listener, so a console
    watching the state feed sees the machine stop rather than the socket
    vanish mid-teardown — ``close()`` can spend a minute releasing hardware,
    and an operator with no console for that minute has no way to see whether
    it happened. On a *restart* the session stays up and only the listener is
    replaced.

    Each cycle gets a fresh
    :class:`~c64cast.control.console_mdns.ConsoleMdnsAdvertiser` rather than
    one updated in place, so a setup completion's flip from pending to
    configured shows up in the TXT record a discovery client reads."""
    from c64cast.control.console_mdns import ConsoleMdnsAdvertiser
    from c64cast.control.control_plane import ControlServer

    try:
        creds = resolve_tokens(web_cfg)
    except RuntimeError as e:
        log.error("%s", e)
        return _Cycle.FAILED

    pending = _setup_pending(web_cfg)
    restart = threading.Event()

    def _on_setup_complete() -> None:
        _clear_setup_reopen()
        restart.set()

    try:
        app = build_daemon_app(
            host.manager,
            host.factory,
            token=creds.token,
            viewer_token=creds.viewer,
            screen_fps=web_cfg.screen_fps,
            log_buffer=host.log_buffer,
            store=host.store,
            library=host.library,
            media=host.media,
            setup_pending=pending,
            token_settable=creds.token_settable,
            on_setup_complete=_on_setup_complete,
        )
        server = ControlServer(web_cfg.host, web_cfg.port, app, label="web console")
    except RuntimeError as e:
        log.error("web console unavailable: %s", e)
        return _Cycle.FAILED

    # Before the banner, the beacon and any autostart: uvicorn binds on its own
    # thread, so a port already in use used to leave the host printing a login
    # URL, autostarting a show on real hardware, and parking forever with
    # nothing listening — then exiting 0.
    if not server.start():
        return _Cycle.FAILED

    mdns = ConsoleMdnsAdvertiser(web_cfg.host, web_cfg.port, pending=pending)
    mdns.start()
    if pending:
        _log_setup_url(web_cfg)
    else:
        _log_console_urls(web_cfg, creds, host.store)

    try:
        if web_cfg.autostart and not pending:
            try:
                host.manager.start(host.factory(None))
            except Exception:
                log.exception("autostart failed; the host is up and idle")
        pump_forever(host.manager, host.shutdown, restart=restart)
    finally:
        if host.shutdown.is_set():
            # `close()` is idempotent, so `run_daemon`'s own call stays the
            # backstop for every other way out of this function.
            host.manager.close()
        mdns.stop()
        server.stop()
    return _Cycle.SHUTDOWN if host.shutdown.is_set() else _Cycle.RESTART


def run_daemon(
    web_cfg: cfgmod.WebCfg,
    load: ConfigLoader,
    *,
    config_path: str = "",
) -> int:
    """Serve the web console until interrupted. The CLI's ``--serve`` body.

    A loop around :func:`_serve_once`, which owns one build-and-pump cycle and
    the shutdown ordering inside it. Normally there is exactly one cycle. With
    ``[web].setup_wizard`` there is a second way out of the pump: the setup
    form finishing (see :mod:`c64cast.control.setup_api`) sets a *restart*
    event rather than the *shutdown* one, and the loop rebuilds the app — this
    time without the setup gate, and with whatever the form just wrote —
    instead of returning. A shutdown signal ends the loop and the process; a
    restart never does.

    Exit codes: ``0`` for a clean shutdown, ``2`` for a cycle that could not
    serve (unreadable credentials, no uvicorn, a port already in use)."""
    log_buffer = SessionLogBuffer()
    log_buffer.install()
    manager = SessionManager(
        settle_s=web_cfg.settle_s, log_buffer=log_buffer, launch_config_path=config_path
    )
    host = _Host(
        manager=manager,
        factory=make_request_factory(load, config_path=config_path),
        log_buffer=log_buffer,
        store=config_store.ConfigStore(web_cfg.config_roots),
        media=media_store.MediaStore(web_cfg.media_read_write, web_cfg.media_read_only),
        library=console_library.ConsoleLibrary(),
        shutdown=threading.Event(),
    )
    _install_signal_handlers(manager, host.shutdown)

    try:
        while True:
            outcome = _serve_once(host, web_cfg)
            if outcome is _Cycle.SHUTDOWN:
                return 0
            if outcome is _Cycle.FAILED:
                return 2
            log.info("web console: setup completed — restarting to pick it up")
    finally:
        manager.close()
        log_buffer.uninstall()
