"""`-v`, `-vv` and `-vvv` are different runs, and a named list is the difference.

urllib3 logs a line per REST request to the Commodore and uvicorn's access log
a line per asset a phone fetches, either of which buries the application's own
DEBUG records — so `configure_logging` holds a list of library loggers at
WARNING and the extra `v`s release it in two steps. `-vv` takes in urllib3 and
uvicorn's server loggers (program-level detail, which for `uvicorn.error`
means INFO and no further); `-vvv` adds uvicorn's access log and its WebSocket
frame debug, both firehose-shaped and both naming what was requested or
pushed. Nothing outside that list has its level moved either way.

`-vv` additionally attaches a filter that drops the records a background poll
loop raises, since those are unconditional and say nothing about the link an
operator is asking about; `-vvv` leaves the filter off. That half is
`TransportFilterTest`.

`AccessLogTest` and `WebSocketFrameLogTest` are the end-to-end half: a real
`ControlServer`, built *after* `configure_logging` — the ordering uvicorn's own
`dictConfig` was breaking — answering a real request and pushing a real frame.
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import patch

from _fakes import FakeAPI

from c64cast import _transport_log
from c64cast.app import cli_commands
from c64cast.hw.backend import C64Backend

try:
    import uvicorn  # noqa: F401

    HAVE_UVICORN = True
except ImportError:
    HAVE_UVICORN = False

try:
    import websockets  # noqa: F401

    HAVE_WEBSOCKETS = True
except ImportError:
    HAVE_WEBSOCKETS = False

# Spelled out rather than imported from the subject, so that a name dropped
# from `cli_commands.HELD_BACK_LOGGERS` fails here instead of agreeing with
# itself.
_TRANSPORT = ("urllib3", "urllib3.connectionpool")
_SERVER = ("uvicorn", "uvicorn.error", "uvicorn.asgi")
_ACCESS = ("uvicorn.access",)
_UVICORN = _SERVER + _ACCESS
_HELD_BACK = _TRANSPORT + _UVICORN


async def _ok_app(
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """The smallest ASGI app that answers a request, pushes a WebSocket frame
    and speaks the lifespan protocol. Raw ASGI rather than FastAPI so that the
    access log and the frame log can be driven with nothing between uvicorn and
    the assertion — and so these tests need only uvicorn installed."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": '{"pushed":"state"}'})
        await receive()
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def _own_levels() -> dict[str, int]:
    """Each existing logger's own level, by name. Root is not in the registry.

    The comprehension walks a copy: `loggerDict` is the interpreter's live
    logger registry, and a `getLogger` from a thread an earlier test module
    left winding down changes its size mid-iteration.
    """
    registry = dict(logging.root.manager.loggerDict)

    return {
        name: logger.level
        for name, logger in registry.items()
        if isinstance(logger, logging.Logger)
    }


def _levels_after(verbosity: int | None) -> dict[str, int]:
    """Every logger's own level after `configure_logging(verbosity)`, measured
    from a cleared registry so that runs are measured alike. `None` skips the
    call, leaving the cleared registry itself to compare a run against.

    Clearing is what makes the comparison mean anything: a level the subject
    writes but never resets otherwise arrives already set from an earlier call
    in the process, and reads as no difference at all.
    """
    before = _own_levels()
    for name in before:
        logging.getLogger(name).setLevel(logging.NOTSET)

    try:
        if verbosity is not None:
            cli_commands.configure_logging(verbosity)
        after = _own_levels()
    finally:
        for name in _own_levels():
            logging.getLogger(name).setLevel(before.get(name, logging.NOTSET))

    return after


class _RestoresLogging(unittest.TestCase):
    """`configure_logging` moves the root logger and every held-back logger, so
    each test restores all of them — those levels and filters belong to the
    process, and the suite's other tests are entitled to find them as they
    were."""

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        saved = {
            name: (logging.getLogger(name).level, logging.getLogger(name).filters[:])
            for name in _HELD_BACK
        }

        def restore() -> None:
            for handler in root.handlers[:]:
                if handler not in handlers:
                    handler.close()
            root.handlers[:] = handlers
            root.setLevel(level)
            for name, (lvl, filters) in saved.items():
                logger = logging.getLogger(name)
                logger.setLevel(lvl)
                logger.filters[:] = filters

        self.addCleanup(restore)


class TransportVerbosityTest(_RestoresLogging):
    def _transport_debug(self) -> list[bool]:
        """Whether a urllib3 DEBUG record would be emitted, root level included."""
        return [logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in _TRANSPORT]

    def test_a_default_run_holds_the_transport_back(self):
        cli_commands.configure_logging(0)
        self.assertEqual(self._transport_debug(), [False, False])

    def test_one_v_holds_it_back_as_well(self):
        cli_commands.configure_logging(1)
        self.assertEqual(self._transport_debug(), [False, False])

    def test_two_vs_release_it(self):
        cli_commands.configure_logging(2)
        self.assertEqual(self._transport_debug(), [True, True])

    def test_three_vs_release_it_too(self):
        cli_commands.configure_logging(3)
        self.assertEqual(self._transport_debug(), [True, True])

    def test_the_second_call_of_a_run_decides(self):
        """`cli.main` configures twice — once on the CLI args, once on the
        loaded config — so a WARNING left standing by the first call would
        outrank the second call's `-vv`."""
        cli_commands.configure_logging(0)
        cli_commands.configure_logging(2)

        self.assertEqual(self._transport_debug(), [True, True])

    def test_and_the_other_way_round(self):
        cli_commands.configure_logging(2)
        cli_commands.configure_logging(0)

        self.assertEqual(self._transport_debug(), [False, False])

    def test_the_second_v_does_not_move_the_application_logger(self):
        cli_commands.configure_logging(1)
        at_one = logging.getLogger("c64cast").isEnabledFor(logging.DEBUG)
        cli_commands.configure_logging(2)
        at_two = logging.getLogger("c64cast").isEnabledFor(logging.DEBUG)

        self.assertEqual((at_one, at_two), (True, True))

    def test_no_other_logger_moves_between_the_first_two_vs(self):
        """The changelog entry and this module both say the second `v` is
        urllib3 plus uvicorn's server loggers and nothing else. Holding back a
        further logger on `-vv` has to fail here, whether the new code clears
        it on the `-v` path or only ever sets it."""
        at_two = _levels_after(2)
        at_one = _levels_after(1)

        moved = {n for n in at_two.keys() | at_one.keys() if at_two.get(n) != at_one.get(n)}
        self.assertEqual(moved, set(_TRANSPORT) | set(_SERVER))

    def test_only_the_two_firehoses_move_between_the_second_and_third_v(self):
        """`-vvv`'s whole effect on the levels is uvicorn's access log plus the
        last step of `uvicorn.error`, which goes from INFO to DEBUG — the poll
        filter it also takes off is not a level. A third firehose parked behind
        the third `v` shows up here."""
        at_three = _levels_after(3)
        at_two = _levels_after(2)

        moved = {n for n in at_three.keys() | at_two.keys() if at_three.get(n) != at_two.get(n)}
        self.assertEqual(moved, set(_ACCESS) | {"uvicorn.error"})

    def test_one_v_holds_back_the_named_list_and_nothing_else(self):
        """The two tests above compare verbosities, so a logger held back at
        both escapes them — and that is the shape of the claim, which is about
        what `configure_logging` holds back rather than about one `v`. Measured
        against not calling it at all, such a logger shows up."""
        untouched = _levels_after(None)
        at_one = _levels_after(1)

        held = {n for n in untouched.keys() | at_one.keys() if untouched.get(n) != at_one.get(n)}
        self.assertEqual(held, set(_HELD_BACK))


class UvicornVerbosityTest(_RestoresLogging):
    """The web console's server loggers arrive at `-vv`, its access log and
    `uvicorn.error`'s DEBUG at `-vvv`. Effective levels rather than own
    levels, because a release writes NOTSET and leaves the root logger to
    decide."""

    def _enabled(self) -> dict[str, bool]:
        return {name: logging.getLogger(name).isEnabledFor(logging.INFO) for name in _UVICORN}

    def _debug_enabled(self) -> dict[str, bool]:
        return {name: logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in _UVICORN}

    def test_a_default_run_holds_every_uvicorn_logger_back(self):
        cli_commands.configure_logging(0)
        self.assertEqual(set(self._enabled().values()), {False})

    def test_one_v_holds_them_back_as_well(self):
        cli_commands.configure_logging(1)
        self.assertEqual(set(self._enabled().values()), {False})

    def test_two_vs_release_the_server_loggers_but_not_the_access_log(self):
        cli_commands.configure_logging(2)
        self.assertEqual(
            self._enabled(),
            {"uvicorn": True, "uvicorn.error": True, "uvicorn.asgi": True, "uvicorn.access": False},
        )

    def test_two_vs_stop_the_error_logger_at_info(self):
        """`uvicorn.error` is where the `websockets` library's per-frame debug
        lands, so `-vv` gets uvicorn's lifecycle lines without it."""
        cli_commands.configure_logging(2)
        self.assertFalse(self._debug_enabled()["uvicorn.error"])

    def test_three_vs_release_the_access_log_too(self):
        cli_commands.configure_logging(3)
        self.assertEqual(set(self._enabled().values()), {True})

    def test_three_vs_release_every_uvicorn_logger_to_debug(self):
        cli_commands.configure_logging(3)
        self.assertEqual(set(self._debug_enabled().values()), {True})

    def test_the_second_call_of_a_run_decides(self):
        """`cli.main` configures twice — once on the CLI args, once on the
        loaded config — so a WARNING left standing by the first call would
        outrank the second call's `-vvv`."""
        cli_commands.configure_logging(0)
        cli_commands.configure_logging(3)

        self.assertEqual(set(self._enabled().values()), {True})

    def test_and_the_other_way_round(self):
        cli_commands.configure_logging(3)
        cli_commands.configure_logging(0)

        self.assertEqual(set(self._enabled().values()), {False})

    def test_a_group_whose_steps_are_written_descending_resolves_the_same(self):
        """`configure_logging` folds a group by taking the last step the run's
        verbosity meets, which reads the literal's order unless it sorts
        first. Without the sort a descending group resolves to the *more*
        verbose level, so `uvicorn.error` written this way would reach NOTSET
        at `-vv` — the WebSocket frame dump the steps exist to hold back.

        Pinned against a literal rather than against the shipped table, which
        is ascending: nothing else here goes red if the sort is dropped, so
        the sort would be deletable-green and it fails in the loud
        direction."""
        descending = ((("uvicorn.error",), ((3, logging.NOTSET), (2, logging.INFO))),)

        got = []
        with patch.object(cli_commands, "HELD_BACK_LOGGERS", descending):
            for verbosity in (1, 2, 3):
                cli_commands.configure_logging(verbosity)
                got.append(logging.getLogger("uvicorn.error").level)

        self.assertEqual(got, [logging.WARNING, logging.INFO, logging.NOTSET])

    @unittest.skipUnless(HAVE_UVICORN, "uvicorn not installed")
    def test_a_server_built_afterwards_does_not_re_pin_them(self):
        """The ordering hazard: `uvicorn.Config.__init__` runs `dictConfig`,
        which is global, so a `ControlServer` built after the command line was
        parsed used to put every level back to uvicorn's own."""
        from c64cast.control.control_plane import ControlServer

        cli_commands.configure_logging(3)
        before = self._enabled()
        ControlServer("127.0.0.1", 0, _ok_app, label="probe")
        after = self._enabled()

        self.assertEqual(before, dict.fromkeys(_UVICORN, True))
        self.assertEqual(after, before)

    @unittest.skipUnless(HAVE_UVICORN, "uvicorn not installed")
    def test_the_server_passes_uvicorn_no_logging_configuration_at_all(self):
        """Each of the three is a way for uvicorn to overwrite what
        `configure_logging` decided: `log_config` runs `dictConfig`,
        `log_level` writes the three server levels, and `access_log=False`
        strips the access handler rather than filtering it."""
        from c64cast.control.control_plane import ControlServer

        cfg = ControlServer("127.0.0.1", 0, _ok_app, label="probe")._cfg

        self.assertEqual((cfg.log_config, cfg.log_level, cfg.access_log), (None, None, True))


class _DrivesAProbeServer(_RestoresLogging):
    """A real `ControlServer` on a loopback port, for the end-to-end halves.

    The server is constructed *after* `configure_logging`, which is the
    ordering `uvicorn.Config.__init__`'s `dictConfig` was breaking."""

    def _start_probe(self) -> tuple[Any, int]:
        """A started probe server and the port it is listening on. The caller
        has already chosen the verbosity, which is the subject."""
        from c64cast.control.control_plane import ControlServer

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        server = ControlServer("127.0.0.1", port, _ok_app, label="probe")
        # Belt for a failing assertion in the caller; `stop()` is idempotent,
        # and the thread sandbox wants the serve thread joined however the
        # test ends.
        self.addCleanup(server.stop)

        self.assertTrue(server.start(), "the probe server did not bind")
        return server, port


@unittest.skipUnless(HAVE_UVICORN, "uvicorn not installed")
class AccessLogTest(_DrivesAProbeServer):
    """One real request through a real `ControlServer`, and what it logged.

    The capture is on the **root** logger rather than on `uvicorn.access`,
    deliberately: the hold-back is a level written on `uvicorn.access` itself,
    and `assertLogs` sets the level of whichever logger it is handed — so
    asserting there would overwrite the thing under test and pass at every
    verbosity. Root is also where the records really go, since the server is
    built to install no handlers of its own."""

    def _drive_one_request(self, request_path: str) -> None:
        """Start a probe server, answer one request from it, stop it again."""
        server, port = self._start_probe()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", request_path)
            self.assertEqual(conn.getresponse().read(), b"ok")
        finally:
            conn.close()
        server.stop()

    def _records_at(self, verbosity: int) -> list[logging.LogRecord]:
        cli_commands.configure_logging(verbosity)
        with self.assertLogs(level=logging.INFO) as cm:
            self._drive_one_request("/api/state?system=all")
        return cm.records

    def _log_file_at(self, verbosity: int, request_path: str) -> str:
        """What `--log-file` holds after one request at this verbosity.

        `assertLogs` is no use here: it replaces the root handlers for the
        duration of its block, and the redacting file handler under test is
        one of them. Dropping the *terminal* handler instead is what keeps the
        run silent, and the file is where the records are verified."""
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        cli_commands.configure_logging(verbosity, log_file=path)
        root = logging.getLogger()
        for handler in list(root.handlers):
            if not isinstance(handler, logging.FileHandler):
                root.removeHandler(handler)
                handler.close()

        self._drive_one_request(request_path)

        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_default_run_logs_nothing_from_uvicorn(self):
        names = [r.name for r in self._records_at(0)]
        self.assertEqual([n for n in names if n.startswith("uvicorn")], [])

    def test_two_vs_show_the_server_coming_up_and_no_access_log(self):
        names = [r.name for r in self._records_at(2)]
        self.assertIn("uvicorn.error", names)
        self.assertNotIn("uvicorn.access", names)

    def test_three_vs_add_the_access_log(self):
        access = [r for r in self._records_at(3) if r.name == "uvicorn.access"]
        self.assertEqual(len(access), 1)
        # The client's ephemeral port leads the line, so the assertion starts
        # after it — the URL is the half that makes this a `-vvv` record.
        self.assertEqual(
            access[0].getMessage().split(" - ", 1)[1],
            '"GET /api/state?system=all HTTP/1.1" 200',
        )

    def test_a_token_in_a_requested_url_is_masked_in_the_log_file(self):
        """The access log names every URL, and the console's login URL carries
        the admin token — so `--log-file`, which outlives the run and is not
        created 0600, has to get the masked form. It does only because
        uvicorn's records now reach the root logger's redacting file handler
        instead of a handler of uvicorn's own."""
        written = self._log_file_at(3, "/login?token=s3cr3t-abcdef")

        self.assertIn('"GET /login?token=REDACTED HTTP/1.1" 200', written)
        self.assertNotIn("s3cr3t-abcdef", written)


@unittest.skipUnless(HAVE_UVICORN and HAVE_WEBSOCKETS, "uvicorn/websockets not installed")
class WebSocketFrameLogTest(_DrivesAProbeServer):
    """The other firehose on `uvicorn.error`, and why that logger stops at
    INFO on the second `v`.

    uvicorn hands `uvicorn.error` to the `websockets` library, which latches
    `logger.isEnabledFor(DEBUG)` once per connection and then logs every
    handshake header and every frame in either direction. The console's state
    feed pushes frames for the length of the run, so releasing that logger all
    the way to DEBUG at `-vv` would bury the lifecycle lines `-vv` is for —
    which is exactly what one connection's worth of records measures here."""

    def _uvicorn_debug_at(self, verbosity: int) -> list[logging.LogRecord]:
        from websockets.sync.client import connect

        cli_commands.configure_logging(verbosity)
        # The whole exchange, teardown included, runs inside the capture:
        # uvicorn logs its shutdown at INFO, and at `-vv` that is on a logger
        # released far enough to reach the terminal handler otherwise.
        with self.assertLogs(level=logging.DEBUG) as cm:
            server, port = self._start_probe()
            with connect(f"ws://127.0.0.1:{port}/perf/ws", open_timeout=5) as ws:
                self.assertEqual(ws.recv(timeout=5), '{"pushed":"state"}')
                ws.send("bye")
            server.stop()
        return [r for r in cm.records if r.name == "uvicorn.error" and r.levelno == logging.DEBUG]

    def test_two_vs_keep_the_frame_log_out(self):
        self.assertEqual(self._uvicorn_debug_at(2), [])

    def test_three_vs_let_the_frame_log_through(self):
        """Which also proves the hold-back above is a level and not the
        `websockets` library declining to log at all."""
        messages = [r.getMessage() for r in self._uvicorn_debug_at(3)]

        self.assertIn('> TEXT \'{"pushed":"state"}\' [18 bytes]', messages)


class TransportFilterTest(_RestoresLogging):
    """The poll hold-back: a transport DEBUG record raised inside a
    `quiet_transport()` block is dropped at `-vv` and kept at `-vvv`."""

    def _filtered(self, name: str) -> bool:
        return any(
            isinstance(f, _transport_log.QuietTransportFilter)
            for f in logging.getLogger(name).filters
        )

    def test_only_two_vs_install_the_filter(self):
        got = []
        for verbosity in (0, 1, 2, 3):
            cli_commands.configure_logging(verbosity)
            got.append([self._filtered(name) for name in _TRANSPORT])
        self.assertEqual(got, [[False, False], [False, False], [True, True], [False, False]])

    def test_a_later_call_removes_an_earlier_calls_filter(self):
        # cli.main configures twice; a -vvv read from a TOML has to undo the
        # filter the first call's -vv installed, or the third v does nothing.
        cli_commands.configure_logging(2)
        cli_commands.configure_logging(3)
        self.assertEqual([self._filtered(name) for name in _TRANSPORT], [False, False])

    def test_the_filter_does_not_stack_across_calls(self):
        for _ in range(3):
            cli_commands.configure_logging(2)
        installed = [
            isinstance(f, _transport_log.QuietTransportFilter)
            for f in logging.getLogger("urllib3.connectionpool").filters
        ]
        self.assertEqual(installed, [True])

    def test_a_poll_read_is_dropped_and_an_ordinary_one_is_not(self):
        cli_commands.configure_logging(2)
        transport = logging.getLogger("urllib3.connectionpool")
        with self.assertLogs(transport, logging.DEBUG) as cm:
            with _transport_log.quiet_transport():
                transport.debug("GET /v1/machine:readmem (the poll)")
            transport.debug("GET /v1/machine:reset (what the operator asked about)")
        self.assertEqual(
            [r.getMessage() for r in cm.records],
            ["GET /v1/machine:reset (what the operator asked about)"],
        )

    def test_a_warning_inside_the_block_still_goes_out(self):
        # A retry or a pool-full warning raised during a poll read is evidence.
        cli_commands.configure_logging(2)
        transport = logging.getLogger("urllib3.connectionpool")
        with self.assertLogs(transport, logging.DEBUG) as cm:
            with _transport_log.quiet_transport():
                transport.warning("Retrying (Retry(total=2))")
        self.assertEqual(len(cm.records), 1)

    def test_three_vs_keep_the_poll_read(self):
        cli_commands.configure_logging(3)
        transport = logging.getLogger("urllib3.connectionpool")
        with self.assertLogs(transport, logging.DEBUG) as cm:
            with _transport_log.quiet_transport():
                transport.debug("GET /v1/machine:readmem (the poll)")
        self.assertEqual(len(cm.records), 1)

    def test_the_application_own_records_are_never_dropped(self):
        cli_commands.configure_logging(2)
        own = logging.getLogger("c64cast.control.keyboard")
        with self.assertLogs(own, logging.DEBUG) as cm:
            with _transport_log.quiet_transport():
                own.debug("read $028D failed")
        self.assertEqual(len(cm.records), 1)


class QuietTransportScopeTest(unittest.TestCase):
    """The mark is scoped to the block and to the thread that entered it."""

    def test_the_mark_is_off_outside_the_block(self):
        self.assertFalse(_transport_log.transport_is_quiet())
        with _transport_log.quiet_transport():
            self.assertTrue(_transport_log.transport_is_quiet())
        self.assertFalse(_transport_log.transport_is_quiet())

    def test_nesting_restores_the_outer_depth(self):
        with _transport_log.quiet_transport():
            with _transport_log.quiet_transport():
                pass
            self.assertTrue(_transport_log.transport_is_quiet())
        self.assertFalse(_transport_log.transport_is_quiet())

    def test_an_exception_does_not_leave_the_thread_quiet(self):
        with self.assertRaises(RuntimeError):
            with _transport_log.quiet_transport():
                raise RuntimeError("boom")
        self.assertFalse(_transport_log.transport_is_quiet())

    def test_another_thread_is_unaffected(self):
        seen: list[bool] = []

        def probe() -> None:
            seen.append(_transport_log.transport_is_quiet())

        with _transport_log.quiet_transport():
            t = threading.Thread(target=probe, name="quiet-probe")
            t.start()
            t.join()
        self.assertEqual(seen, [False])


class _QuietProbe:
    """Records whether each read landed inside a `quiet_transport()` block."""

    def __init__(self):
        self.quiet: list[bool] = []

    def read_memory(self, addr, count=1):
        self.quiet.append(_transport_log.transport_is_quiet())
        return bytes(count)

    def write_memory(self, *a, **kw):
        return None


class PolledReadsAreQuietTest(unittest.TestCase):
    """Every unconditional periodic reader takes its read inside a
    `quiet_transport()` block. These are the tree's three: the key poller, the
    launcher's idle detector, and the host-DMA audio servo. A fourth added
    without the wrapper puts the `-vv` flood back, so each site is pinned
    rather than left to the filter's own tests."""

    def test_the_key_poller_reads_quietly(self):
        from c64cast.control.keyboard import CommodoreKeyPoller

        probe = _QuietProbe()
        poller = CommodoreKeyPoller(cast(C64Backend, probe))
        poller._read_modifiers()
        poller._drain_kbbuf()
        self.assertTrue(probe.quiet and all(probe.quiet), probe.quiet)

    def test_the_launcher_idle_detector_reads_quietly(self):
        from c64cast.scenes.scenes import LauncherScene

        tmp = tempfile.mkdtemp()
        prg = os.path.join(tmp, "demo.prg")
        with open(prg, "wb") as f:
            f.write(b"\x01\x08")
        probe = _QuietProbe()
        scene = LauncherScene(cast(C64Backend, probe), prg, input_source="auto")
        scene._read_snapshot()
        self.assertTrue(probe.quiet and all(probe.quiet), probe.quiet)

    def test_the_audio_servo_reads_quietly(self):
        from c64cast.audio.audio import RING_BUFFER_ADDR, AudioStreamer

        probe = _QuietProbe()
        streamer = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC", dither=False)
        streamer.api = cast(C64Backend, probe)
        streamer.host_dma_servo = True
        streamer.servo.next_pace_increment(RING_BUFFER_ADDR + 4096, 0.1)
        self.assertTrue(probe.quiet and all(probe.quiet), probe.quiet)

    def test_the_audio_arm_verification_read_is_not_quiet(self):
        """`read_consumer_ptr`'s other callers are one-shot — the NMI arm
        verification and the pause stomp — and a run that lost its audio is
        exactly when those reads are the evidence `-vv` was asked for."""
        from c64cast.audio.audio import AudioStreamer

        probe = _QuietProbe()
        streamer = AudioStreamer(cast(C64Backend, FakeAPI()), 8000, "NTSC", dither=False)
        streamer.api = cast(C64Backend, probe)
        streamer.read_consumer_ptr()
        self.assertEqual(probe.quiet, [False])


if __name__ == "__main__":
    unittest.main()
