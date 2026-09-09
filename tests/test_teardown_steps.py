"""`run_teardown_steps` — the guarded step runner the SID scene teardowns use.

A scene's teardown steps are independent promises to the next scene, not a
transaction. This module pins the property those scenes rely on: a step that
raises does not starve the steps after it.
"""

from __future__ import annotations

import logging
import unittest

from c64cast.scenes.scenes import run_teardown_steps

log = logging.getLogger("c64cast.tests.teardown_steps")


def _boom() -> None:
    raise RuntimeError("step failed")


class RunTeardownStepsTests(unittest.TestCase):
    def test_every_step_runs_in_order(self):
        ran: list[str] = []
        run_teardown_steps(
            log,
            "Scene",
            [("first", lambda: ran.append("first")), ("second", lambda: ran.append("second"))],
        )
        self.assertEqual(ran, ["first", "second"])

    def test_a_failing_step_does_not_starve_the_steps_after_it(self):
        ran: list[str] = []
        with self.assertLogs(log, level="ERROR"):
            run_teardown_steps(
                log,
                "Scene",
                [
                    ("silence", _boom),
                    ("display restore", lambda: ran.append("display restore")),
                    ("flush", lambda: ran.append("flush")),
                ],
            )
        self.assertEqual(ran, ["display restore", "flush"])

    def test_every_step_can_fail_without_stopping_the_run(self):
        with self.assertLogs(log, level="ERROR") as caught:
            run_teardown_steps(log, "Scene", [("a", _boom), ("b", _boom), ("c", _boom)])
        self.assertEqual(len(caught.records), 3)

    def test_the_failing_step_is_named_in_the_log(self):
        with self.assertLogs(log, level="ERROR") as caught:
            run_teardown_steps(log, "AsidScene", [("kernal IRQ restore", _boom)])
        self.assertIn("AsidScene", caught.output[0])
        self.assertIn("kernal IRQ restore", caught.output[0])
        self.assertIn("RuntimeError", caught.output[0])  # exc_info is attached

    def test_an_interrupt_is_not_swallowed(self):
        # Teardown runs on the shutdown path. Catching Exception (not
        # BaseException) is what keeps a KeyboardInterrupt from being logged as
        # a failed step and then discarded, which would hang the shutdown.
        def interrupt() -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_teardown_steps(log, "Scene", [("interrupted", interrupt)])
