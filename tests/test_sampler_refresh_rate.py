"""What the sampler's deadline refresh costs the link
(c64cast/audio/sampler.py, _advance_deadline): one flush per refresh, and
few refreshes for a live stream near the watermark. A length write the link
lost is found by the next refresh's flush, and the restart covers it."""

from __future__ import annotations

import logging
import unittest
from typing import Any
from unittest import mock

from test_sampler_deadline import LENGTH, _Channel, _run

SECONDS = 30.0


class RefreshCostTest(unittest.TestCase):
    def test_a_live_stream_near_the_watermark(self):
        # A stream a little past the watermark moves the deadline in small
        # steps. Each refresh flushed twice, about 22 flushes a second at
        # 44.1 kHz/16-bit (10.9 refreshes), on a link of about 200 round
        # trips a second that the picture shares.
        for rate, bits in ((44100, 16), (48000, 16), (22050, 8)):
            with self.subTest(rate=rate, bits=bits):
                with self.assertNoLogs("c64cast.audio.sampler", logging.WARNING):
                    smp, chan, _ = _run(rate=rate, bits=bits, seconds=SECONDS, ahead_s=0.3)
                self.assertLessEqual(chan.flushes, chan.length_writes + 3)
                self.assertLess(chan.flushes / SECONDS, 10.0)
                self.assertEqual((smp._restarts, chan.hazards, chan.stale), (0, 0, 0))

    def test_a_decoder_a_lead_ahead(self):
        # Each length write is confirmed before the voice nears the deadline
        # ahead of it, so two flushes fit in every lead less two guards:
        # about 2.2 a second at a 1 s lead, where two per refresh cost 2.8.
        smp, chan, _ = _run(seconds=SECONDS)
        self.assertLess(chan.flushes / SECONDS, 2.5)
        self.assertEqual((smp._restarts, chan.hazards, chan.stale), (0, 0, 0))


class _QuietlyLosingChannel(_Channel):
    """Drops the ``nth`` length write without a word, the way a write is lost
    when its connection drops after the send: the next flush charges it."""

    def __init__(self, *a: Any, nth: int, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.nth = nth
        self.seen = 0

    def write_regs(self, base_addr: str, *values: int) -> None:
        if int(base_addr, 16) == LENGTH:
            self.seen += 1
            if self.seen == self.nth:
                self.advance()
                self.dropped = True
                return
        super().write_regs(base_addr, *values)


class DeferredConfirmationTest(unittest.TestCase):
    def test_a_length_write_lost_after_its_send_is_covered_by_a_restart(self):
        # Found one refresh later: the refresh sets the deadline back to the
        # one the voice is still playing toward, so the restart lands inside
        # the guard before the voice runs out, and plays nothing stale. A
        # decoder a lead ahead is confirmed by the refresh _deadline_confirm_by
        # brings forward; waiting for its next step lost about half a second.
        made: list[_QuietlyLosingChannel] = []

        def channel(*a: Any, **kw: Any) -> _QuietlyLosingChannel:
            made.append(_QuietlyLosingChannel(*a, nth=5, **kw))
            return made[-1]

        for ahead in (0.3, 0.6, None):
            with self.subTest(ahead=ahead):
                made.clear()
                with (
                    mock.patch("test_sampler_deadline._Channel", channel),
                    self.assertLogs("c64cast.audio.sampler", logging.WARNING) as logs,
                ):
                    smp, chan, _ = _run(seconds=SECONDS, ahead_s=ahead)
                self.assertIs(chan, made[0])
                self.assertGreaterEqual(made[0].seen, 5)
                self.assertIsNone(chan.finished_at)
                self.assertIn("reached its deadline", "\n".join(logs.output))
                self.assertEqual(smp._restarts, 1)
                self.assertEqual((chan.state, chan.stale, chan.hazards), ("playing", 0, 0))

    def test_the_deadline_never_moves_past_unconfirmed_ring_audio(self):
        # A ring write charged lost at the refresh's flush holds the deadline
        # where it was confirmed.
        drops = iter([False] * 300 + [True])
        with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
            smp, chan, _ = _run(seconds=SECONDS, drop_ring=lambda: next(drops, False))
        self.assertEqual(smp._restarts, 1)
        self.assertEqual((chan.state, chan.stale), ("playing", 0))


if __name__ == "__main__":
    unittest.main()
