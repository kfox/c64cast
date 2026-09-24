"""`-v`, `-vv` and `-vvv` are different runs, and one logger is the difference.

urllib3 logs a line per REST request to the Commodore, which buries the
application's own DEBUG records — so `configure_logging` holds it at WARNING,
and `-vv` is what releases it. No other logger's *level* moves, so that
release is the whole of what the second `v` does to the levels.

`-vv` additionally attaches a filter that drops the records a background poll
loop raises, since those are unconditional and say nothing about the link an
operator is asking about; `-vvv` leaves the filter off. That half is
`TransportFilterTest`.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import unittest
from typing import cast

from _fakes import FakeAPI

from c64cast import _transport_log
from c64cast.app import cli_commands
from c64cast.hw.backend import C64Backend

_TRANSPORT = ("urllib3", "urllib3.connectionpool")


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


class TransportVerbosityTest(unittest.TestCase):
    """`configure_logging` moves the root logger and the transport loggers, so
    each test restores both — the transport levels belong to the process, and
    the suite's other tests are entitled to find them as they were."""

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        levels = {name: logging.getLogger(name).level for name in _TRANSPORT}

        def restore() -> None:
            for handler in root.handlers[:]:
                if handler not in handlers:
                    handler.close()
            root.handlers[:] = handlers
            root.setLevel(level)
            for name, saved in levels.items():
                logging.getLogger(name).setLevel(saved)

        self.addCleanup(restore)

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

    def test_the_second_v_moves_nothing_but_the_transport(self):
        cli_commands.configure_logging(1)
        at_one = logging.getLogger("c64cast").isEnabledFor(logging.DEBUG)
        cli_commands.configure_logging(2)
        at_two = logging.getLogger("c64cast").isEnabledFor(logging.DEBUG)

        self.assertEqual((at_one, at_two), (True, True))

    def test_no_other_logger_moves_between_the_two(self):
        """The changelog entry and this module both say urllib3 is the whole of
        the difference. Holding back a second logger on `-vv` has to fail here,
        whether the new code clears it on the `-v` path or only ever sets it."""
        at_two = _levels_after(2)
        at_one = _levels_after(1)

        moved = {n for n in at_two.keys() | at_one.keys() if at_two.get(n) != at_one.get(n)}
        self.assertEqual(moved, set(_TRANSPORT))

    def test_one_v_holds_back_the_transport_and_nothing_else(self):
        """The test above compares the two verbosities, so a logger held back
        at both escapes it — and that is the shape of the claim, which is about
        what `configure_logging` holds back rather than about the second `v`.
        Measured against not calling it at all, such a logger shows up."""
        untouched = _levels_after(None)
        at_one = _levels_after(1)

        held = {n for n in untouched.keys() | at_one.keys() if untouched.get(n) != at_one.get(n)}
        self.assertEqual(held, set(_TRANSPORT))


class TransportFilterTest(unittest.TestCase):
    """The poll hold-back: a transport DEBUG record raised inside a
    `quiet_transport()` block is dropped at `-vv` and kept at `-vvv`."""

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        saved = {
            name: (logging.getLogger(name).level, logging.getLogger(name).filters[:])
            for name in _TRANSPORT
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
