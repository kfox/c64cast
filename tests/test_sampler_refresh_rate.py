"""What the sampler's deadline refresh costs the link
(c64cast/audio/sampler.py, _advance_deadline): one flush per refresh, and
few refreshes for a live stream near the watermark. A length write the link
lost is found by the next refresh's flush, and the restart covers it."""

from __future__ import annotations

import logging
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np
from test_sampler_deadline import LENGTH, RING_BASE, _Channel, _run, _Writer
from test_sampler_long_outage import _SteppedClock, _WaitingQueue

from c64cast.audio import sampler as s

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
    when its connection drops after the send: the next flush charges it.
    That flush sits in the transport for ``stall`` seconds, a redial."""

    def __init__(self, *a: Any, nth: int, stall: float = 0.0, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.nth = nth
        self.stall = stall
        self.seen = 0

    def flush(self) -> None:
        if self.dropped:
            self.clock.now += self.stall
        super().flush()

    def write_regs(self, base_addr: str, *values: int) -> None:
        if int(base_addr, 16) == LENGTH:
            self.seen += 1
            if self.seen == self.nth:
                self.advance()
                self.dropped = True
                return
        super().write_regs(base_addr, *values)


def _looped_run(
    *, ahead_s: float, nth: int, stall: float = 0.0, seconds: float = 6.0
) -> _QuietlyLosingChannel:
    """The real writer loop on a clock its own sleeps and queue waits
    advance, a producer ``ahead_s`` ahead, and the ``nth`` length write lost
    after its send: `_run` steps the writer every 10 ms whatever it slept."""
    clock = _SteppedClock()
    smp = s.UltimateAudioSampler(
        cast(Any, None),
        sample_rate=8000,
        bits=16,
        ring_base=RING_BASE,
        ring_size=0x30000,
        ref_clock_hz=s.SAMPLER_REF_CLOCK_DEFAULT,
    )
    chan = _QuietlyLosingChannel(
        clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2, nth=nth, stall=stall
    )
    smp.api = cast(Any, chan)
    smp._q = cast(Any, _WaitingQueue(clock))
    tone = np.full(80, 8000, dtype=np.int16)  # 10 ms
    produced = [0.0]

    def produce(until: float) -> None:
        while produced[0] < until:
            smp.push_samples(tone)
            produced[0] += 0.01

    def tick() -> None:
        produce(clock.now + ahead_s)
        chan.advance()
        if clock.now >= seconds:
            smp._running = False

    with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
        # At least the prebuffer: start() waits for it on a clock that does not pass.
        produce(max(ahead_s, 0.6))
        smp.start(prebuffer_timeout=1.0)
        clock.tick = tick
        smp._writer_loop(smp._writer_gen)
        smp.stop()
    return chan


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

    def test_the_writers_back_off_never_outlasts_the_voice(self):
        # The refresh that finds the loss raises, and the writer backs off:
        # slept past the pass that restarts the channel, on top of a ring
        # pass waiting out a gather, it stopped the voice 40 to 80 ms first.
        for ahead in (0.6, 1.0):
            for nth in (3, 9):
                with self.subTest(ahead=ahead, nth=nth):
                    with self.assertLogs("c64cast.audio.sampler", logging.WARNING) as logs:
                        chan = _looped_run(ahead_s=ahead, nth=nth)
                    self.assertGreaterEqual(chan.seen, nth)
                    self.assertIn("reached its deadline", "\n".join(logs.output))
                    self.assertIsNone(chan.finished_at)

    def test_a_refresh_that_raises_inside_the_guard_restarts_at_once(self):
        # The flush that finds the loss sits in a redial until the read head
        # is inside the guard, where the back-off is no longer cut short: the
        # 20 ms it slept before the restart stopped the voice.
        for nth in (3, 9):
            with self.subTest(nth=nth):
                with self.assertLogs("c64cast.audio.sampler", logging.WARNING) as logs:
                    chan = _looped_run(ahead_s=0.6, nth=nth, stall=0.085)
                self.assertGreaterEqual(chan.seen, nth)
                output = "\n".join(logs.output)
                self.assertIn("reached its deadline", output)
                self.assertIn("deadline write; restarting the channel", output)
                self.assertIsNone(chan.finished_at)

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
