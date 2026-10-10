"""How the sampler's writer reports and accounts for writes the link lost
(c64cast/audio/sampler.py): the log line names the write that was lost."""

from __future__ import annotations

import logging
import unittest
from typing import Any, cast
from unittest import mock

from c64cast.audio import sampler as s


class _NoSleep:
    """The sampler module's ``time`` with sleeps that do not wait."""

    @staticmethod
    def sleep(_s: float) -> None:
        pass

    @staticmethod
    def monotonic() -> float:
        return 0.0


class FailureMessageTest(unittest.TestCase):
    def _loop_once(self, error: Exception) -> list[str]:
        """Run the writer loop through one failed step and one that writes,
        returning what it logged."""
        smp = s.UltimateAudioSampler(cast(Any, None), sample_rate=8000, bits=16)
        smp._running = True
        steps: list[Exception | bool] = [error, True]

        def step(_gen: int) -> bool:
            if not steps:
                smp._running = False
                return False
            outcome = steps.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            mock.patch.object(smp, "_writer_step", step),
            mock.patch.object(s, "time", _NoSleep),
            self.assertLogs("c64cast.audio.sampler", logging.INFO) as logs,
        ):
            smp._writer_loop(smp._writer_gen)
        return logs.output

    def test_a_lost_length_write_is_not_called_a_ring_write(self):
        # A deadline refresh whose length write the link lost raises from the
        # loss check, not from a REU write.
        logs = self._loop_once(s._WritesLost("the link lost a deadline (length register) write"))
        warning = next(m for m in logs if m.startswith("WARNING"))
        self.assertIn("lost a deadline (length register) write", warning)
        self.assertNotIn("ring write failed", warning)
        self.assertTrue(any("writes recovered" in m for m in logs), logs)

    def test_a_transport_error_is_the_ring_writes(self):
        logs = self._loop_once(ConnectionError("link down"))
        warning = next(m for m in logs if m.startswith("WARNING"))
        self.assertIn("ring write failed (link down)", warning)

    def test_the_give_up_names_the_last_failure(self):
        smp = s.UltimateAudioSampler(cast(Any, None), sample_rate=8000, bits=16)
        with (
            mock.patch.object(smp, "_gate_off_landed", return_value=None),
            self.assertLogs("c64cast.audio.sampler", logging.ERROR) as logs,
        ):
            smp._give_up(s._WritesLost("the link lost ring audio"), smp._writer_gen)
        self.assertIn("(last: the link lost ring audio)", logs.output[0])
        self.assertNotIn("ring write", logs.output[0])


if __name__ == "__main__":
    unittest.main()
