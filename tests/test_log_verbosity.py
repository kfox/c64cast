"""`-v` and `-vv` are different runs, and the difference is one logger.

urllib3 logs a line per REST request to the Commodore, which buries the
application's own DEBUG records — so `configure_logging` holds it at WARNING,
and `-vv` is what releases it. `configure_logging` holds back no other
logger, so that release is the whole of what the second `v` does.
"""

from __future__ import annotations

import logging
import unittest

from c64cast.app import cli_commands

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


if __name__ == "__main__":
    unittest.main()
