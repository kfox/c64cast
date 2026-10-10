"""A link outage longer than the sampler writer's give-up
(c64cast/audio/sampler.py, WRITER_GIVE_UP_S): the writer stops taking audio
but not running, and brings the channel back once the link answers.

The real writer loop runs on a fake clock that its own sleeps and queue
waits advance, against test_sampler_deadline's model of the FPGA channel,
with a real-time producer pushing through ``push_samples``."""

from __future__ import annotations

import logging
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np
from test_sampler_deadline import RING_BASE, _Channel, _Writer

from c64cast.audio import sampler as s


class _SteppedClock:
    """The sampler module's ``time``: sleeps advance it, and every advance
    runs ``tick`` (the producer, the link, the channel model)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.tick: Any = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)
        if self.tick is not None:
            self.tick()


class _WaitingQueue:
    """The sampler's queue, whose empty waits pass on the fake clock."""

    def __init__(self, clock: _SteppedClock) -> None:
        self.clock = clock
        self.items: list[Any] = []

    def put(self, item: Any, *_a: Any, **_kw: Any) -> None:
        self.items.append(item)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        if not self.items and block and timeout:
            self.clock.sleep(timeout)
        if not self.items:
            raise s.queue.Empty
        return self.items.pop(0)

    def get_nowait(self) -> Any:
        return self.get(block=False)

    def empty(self) -> bool:
        return not self.items


def _outage_run(outage: tuple[float, float], seconds: float) -> dict[str, Any]:
    """Play a real-time producer (a lead ahead) through the writer loop for
    ``seconds`` with the link down over ``outage``; returns what was seen."""
    clock = _SteppedClock()
    smp = s.UltimateAudioSampler(
        cast(Any, None),
        sample_rate=8000,
        bits=16,
        ring_base=RING_BASE,
        ring_size=0x30000,
        ref_clock_hz=s.SAMPLER_REF_CLOCK_DEFAULT,
    )
    chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)  # type: ignore[arg-type]
    smp.api = cast(Any, chan)
    smp._q = cast(Any, _WaitingQueue(clock))
    frame = 80  # 10 ms
    tone = np.full(frame, 8000, dtype=np.int16)
    seen: dict[str, Any] = {
        "played_in_outage": 0.0,  # seconds the channel played past the lead
        "back_after": None,
        "accepted_after": 0,
        "failed_seen": False,
    }
    produced = [0.0]

    def tick() -> None:
        now = clock.now
        chan.down = outage[0] <= now < outage[1]
        while produced[0] < now + 1.0:
            accepted = smp.push_samples(tone)
            produced[0] += frame / smp._actual_rate
            if now >= outage[1] and seen["back_after"] is not None:
                seen["accepted_after"] += accepted
        chan.advance()
        seen["failed_seen"] = seen["failed_seen"] or smp._failed
        if outage[0] + 1.2 <= now < outage[1] and chan.state == "playing":
            seen["played_in_outage"] += 0.001
        if now >= outage[1] and chan.state == "playing" and seen["back_after"] is None:
            seen["back_after"] = now - outage[1]
        if now >= seconds and smp._running:
            # Where the writer thinks the channel reads, against where it
            # does, taken before the stop zeroes the read head.
            ring = smp.ring_size
            drift = (chan.played - smp._ring_off(smp._read_consumed_bytes())) % ring
            seen["offset_error_s"] = min(drift, ring - drift) / 2 / smp._actual_rate
            smp._running = False

    with (
        mock.patch.object(s, "time", clock),
        mock.patch.object(s, "PollThread", _Writer),
    ):
        tick()
        smp.start(prebuffer_timeout=1.0)
        clock.tick = tick
        smp._writer_loop(smp._writer_gen)
        seen["failed_at_end"] = smp._failed
        seen["stale"] = chan.stale
        seen["state"] = chan.state
        seen["reanchors"] = smp._reanchors
        smp.stop()
    return seen


class LongOutageTest(unittest.TestCase):
    def test_past_the_give_up_the_sound_comes_back_with_the_link(self):
        with self.assertLogs("c64cast.audio.sampler", logging.INFO) as logs:
            seen = _outage_run(outage=(5.0, 20.0), seconds=30.0)
        text = "\n".join(logs.output)
        self.assertIn("dropping audio", text)  # it did give up
        self.assertTrue(seen["failed_seen"])
        # Silent through the outage once the lead ran out.
        self.assertEqual(seen["played_in_outage"], 0.0)
        # Back within the back-off's ceiling of the link answering, with the
        # producer's audio taken again and nothing stale played.
        self.assertIsNotNone(seen["back_after"], text)
        self.assertLessEqual(seen["back_after"], s.WRITER_BACKOFF_MAX_S + 0.1)
        self.assertGreater(seen["accepted_after"], 0)
        self.assertFalse(seen["failed_at_end"])
        self.assertEqual((seen["state"], seen["stale"]), ("playing", 0))
        self.assertLess(seen["offset_error_s"], 0.001)
        self.assertIn("taking audio again", text)
        # The audio kept its anchor through the give-up: a producer a lead
        # ahead is on time again at once, so nothing is re-anchored behind
        # the picture.
        self.assertEqual(seen["reanchors"], 0)

    def test_a_short_outage_comes_back_without_a_give_up(self):
        with self.assertLogs("c64cast.audio.sampler", logging.INFO) as logs:
            seen = _outage_run(outage=(5.0, 8.0), seconds=15.0)
        self.assertNotIn("dropping audio", "\n".join(logs.output))
        self.assertFalse(seen["failed_seen"])
        self.assertEqual(seen["played_in_outage"], 0.0)
        self.assertIsNotNone(seen["back_after"])
        self.assertEqual((seen["state"], seen["stale"]), ("playing", 0))


if __name__ == "__main__":
    unittest.main()
