"""The sampler's dead-man deadline (c64cast/audio/sampler.py, DEADLINE_GUARD_S).

The channel's length register stops the FPGA even inside its A↔B loop, so the
writer keeps it at the end of what the ring holds for this lap: a link that
stops carrying writes stops the sound there instead of the loop replaying the
last lap. These tests run the real writer on a fake clock against a model of
the FPGA channel (sampler2.vhd's voice state machine), which plays the ring
at the sample rate, stops where its position meets the length register,
applies a length write byte by byte, and counts every byte it plays that was
not written since the last time it played that slot."""

from __future__ import annotations

import logging
import random
import threading
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np

from c64cast.audio import sampler as s

RING_BASE = 0x200000
CTRL = s.channel_base(0)
LENGTH = CTRL + s.REG_LENGTH


class _Clock:
    """The sampler module's ``time`` on a fake clock: sleeps do not pass it."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, _s: float) -> None:
        pass

    def monotonic(self) -> float:
        return self.now


class _Queue:
    """The sampler's queue without its blocking waits."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def put(self, item: Any, *_a: Any, **_kw: Any) -> None:
        self.items.append(item)

    def get(self, *_a: Any, **_kw: Any) -> Any:
        if not self.items:
            raise s.queue.Empty
        return self.items.pop(0)

    get_nowait = get

    def empty(self) -> bool:
        return not self.items


class _Writer:
    """Stands in for the writer's PollThread: the test steps the writer."""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_running(self) -> bool:
        return False


class _Channel:
    """Sampler channel 0 and the REU ring behind it, on ``clock``.

    ``down`` fails every write: a REU write raises, and a register write or a
    flush is lost and moves ``delivery_epoch``, as the Ultimate's backend
    does. ``lose`` loses one register write when it returns True; ``foreign``
    counts a loss on some other thread at a register write that lands, moving
    ``delivery_epoch`` but not this thread's loss mark. ``drop_ring`` loses a
    REU write without raising, counted lost at the next flush, as an
    unanswered IDENTIFY charges the writes it could not confirm. ``ahead``
    is how far (s) the FPGA runs ahead of the moment its gate-on lands."""

    def __init__(
        self, clock: _Clock, *, ring: int, byte_rate: float, bps: int, ahead: float = 0.0
    ) -> None:
        self.clock = clock
        self.ring = ring
        self.byte_rate = byte_rate
        self.bps = bps
        self.ahead = ahead
        self.delivery_epoch = 0
        self.down = False
        self.lose: Any = None
        self.foreign: Any = None
        self.foreign_losses = 0
        self.drop_ring: Any = None
        self.dropped = False  # a dropped ring write the next flush charges
        self.flushes = 0
        self.ctrl = 0
        self.length = [0, 0, 0]
        self.state = "idle"
        self.gate_t = 0.0
        self.played = 0  # bytes played since the gate-on
        self.written_at = np.full(ring, -np.inf)
        self.stale = 0
        self.hazards = 0
        self.gates = 0
        self.finished_at: float | None = None
        self.length_writes = 0

    def _position(self) -> int:
        t = self.clock.now - self.gate_t + self.ahead
        return int(t * self.byte_rate / self.bps) * self.bps

    def advance(self) -> None:
        if self.state != "playing":
            return
        p0, p1 = self.played, self._position()
        if p1 <= p0:
            return
        length = int.from_bytes(bytes(self.length), "big")
        if 0 < length < self.ring:
            hit = p0 - p0 % self.ring + length
            if hit <= p0:
                hit += self.ring
            if hit <= p1:
                p1 = hit
                self.state = "finished"
                self.finished_at = self.clock.now
        pos = np.arange(p0, p1)
        played_at = self.gate_t - self.ahead + pos / self.byte_rate
        last_lap = np.where(pos >= self.ring, played_at - self.ring / self.byte_rate, -np.inf)
        self.stale += int(np.count_nonzero(self.written_at[pos % self.ring] <= last_lap))
        self.played = p1

    def _lost(self) -> bool:
        if self.foreign is not None and self.foreign():
            self.delivery_epoch += 1
            self.foreign_losses += 1
        if self.down or (self.lose is not None and self.lose()):
            self.delivery_epoch += 1
            return True
        return False

    def write_loss_mark(self) -> int:
        return self.delivery_epoch - self.foreign_losses

    def writes_lost_since(self, mark: int) -> bool:
        return self.write_loss_mark() != mark

    def reu_write(self, offset: int, data: bytes) -> None:
        self.advance()
        if self.down:
            raise ConnectionError("link down")
        if self.drop_ring is not None and self.drop_ring():
            self.dropped = True
            return
        at = offset - RING_BASE
        self.written_at[at : at + len(data)] = self.clock.now

    def write_regs(self, base_addr: str, *values: int) -> None:
        self.advance()
        if self._lost():
            return
        addr = int(base_addr, 16)
        if addr != LENGTH:
            return
        self.length_writes += 1
        if self.state == "playing":
            near = int(0.02 * self.byte_rate)
            here = self.played % self.ring
            for order in ((0, 1, 2), (2, 1, 0)):
                reg = list(self.length)
                for i in order[:-1]:
                    reg[i] = values[i]
                    value = int.from_bytes(bytes(reg), "big")
                    if (
                        0 < value < self.ring
                        and min((value - here) % self.ring, (here - value) % self.ring) <= near
                    ):
                        self.hazards += 1
        self.length = list(values)

    def write_memory(self, address: str, data_hex: str) -> None:
        self.advance()
        if self._lost():
            return
        if int(address, 16) != CTRL:
            return
        value = int(data_hex, 16)
        if not value & s.CTRL_GATE:
            self.state = "idle"
        elif self.state == "idle":
            self.state = "playing"
            self.gate_t = self.clock.now
            self.played = 0
            self.gates += 1
        self.ctrl = value

    def flush(self) -> None:
        self.advance()
        self.flushes += 1
        if self.down or self.dropped:
            self.delivery_epoch += 1
        self.dropped = False


def _run(
    *,
    rate: int = 44100,
    bits: int = 16,
    lead: float = 1.0,
    ring: int = 0x30000,
    seconds: float = 30.0,
    outage: tuple[float, float] | None = None,
    ahead: float = 0.0,
    lose: Any = None,
    foreign: Any = None,
    drop_ring: Any = None,
    frame_s: float = 0.01,
    ahead_s: float | None = None,
) -> tuple[s.UltimateAudioSampler, _Channel, dict[str, Any]]:
    """Play a real-time producer through the real writer for ``seconds``,
    delivering ``ahead_s`` (default: the lead) ahead of the read head, as a
    file decoder does; a live stream sits nearer. Returns the sampler, the channel, and when the channel finished and
    played again around the ``outage``."""
    clock = _Clock()
    smp = s.UltimateAudioSampler(
        cast(Any, None),
        sample_rate=rate,
        bits=bits,
        lead_seconds=lead,
        ring_base=RING_BASE,
        ring_size=ring,
        ref_clock_hz=s.SAMPLER_REF_CLOCK_DEFAULT,
    )
    bps = smp.bps
    chan = _Channel(
        clock, ring=smp.ring_size, byte_rate=smp._actual_rate * bps, bps=bps, ahead=ahead
    )
    chan.lose = lose
    chan.foreign = foreign
    chan.drop_ring = drop_ring
    smp.api = cast(Any, chan)
    q = _Queue()
    smp._q = cast(Any, q)
    frame = max(1, int(frame_s * smp._actual_rate))
    ahead_s = lead if ahead_s is None else ahead_s
    produced = 0.0
    # At least the prebuffer: start() waits for it, and the fake clock does
    # not pass while it does.
    while produced < max(ahead_s, 0.6):
        q.put((smp._flush_epoch, b"\x01" * (frame * bps)))
        produced += frame / smp._actual_rate
    seen: dict[str, Any] = {"finished_in_outage": None, "playing_after": None}
    with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
        smp.start(prebuffer_timeout=1.0)
        for tick in range(int(round(seconds / frame_s))):
            clock.now = tick * frame_s
            chan.down = outage is not None and outage[0] <= clock.now < outage[1]
            while produced < clock.now + ahead_s:
                q.put((smp._flush_epoch, b"\x01" * (frame * bps)))
                produced += frame / smp._actual_rate
            for _ in range(50):
                try:
                    wrote = smp._writer_step(smp._writer_gen)
                except ConnectionError:
                    break
                if not wrote and q.empty():
                    break
            chan.advance()
            if outage is not None:
                if chan.down and chan.state == "finished" and seen["finished_in_outage"] is None:
                    seen["finished_in_outage"] = clock.now - outage[0]
                back = clock.now >= outage[1] and chan.state == "playing"
                if back and seen["playing_after"] is None:
                    seen["playing_after"] = clock.now - outage[1]
        # Where the writer thinks the channel is reading, against where it is:
        # off by more than the FPGA's lead, every sample lands out of place.
        ring = smp.ring_size
        drift = (chan.played - smp._ring_off(smp._read_consumed_bytes())) % ring
        seen["offset_error_s"] = min(drift, ring - drift) / bps / smp._actual_rate
    return smp, chan, seen


class LengthIntermediatesTest(unittest.TestCase):
    def test_every_mix_of_old_and_new_bytes_but_the_two_ends(self):
        self.assertEqual(
            s.length_write_intermediates(0x01_02_03, 0x0A_0B_0C),
            {0x0A_02_03, 0x01_0B_03, 0x01_02_0C, 0x0A_0B_03, 0x0A_02_0C, 0x01_0B_0C},
        )

    def test_a_one_byte_change_passes_through_nothing(self):
        self.assertEqual(s.length_write_intermediates(0x05_10_00, 0x05_20_00), set())


class HealthyLinkTest(unittest.TestCase):
    """A link that carries every write: the deadline never stops the channel,
    no length write passes through the play position, and nothing stale plays,
    for a decoder a lead ahead and for a live stream 0.2 s ahead."""

    def test_rates_widths_and_leads(self):
        for rate in (8000, 22050, 32000, 44100, 48000):
            for bits in (8, 16):
                for lead, ahead in ((0.5, None), (1.0, None), (2.0, None), (1.0, 0.2)):
                    with self.subTest(rate=rate, bits=bits, lead=lead, ahead=ahead):
                        with self.assertNoLogs("c64cast.audio.sampler", logging.WARNING):
                            smp, chan, seen = _run(
                                rate=rate, bits=bits, lead=lead, seconds=40.0, ahead_s=ahead
                            )
                        self.assertLess(seen["offset_error_s"], 0.001)
                        self.assertEqual(chan.state, "playing")
                        self.assertEqual(chan.gates, 1)
                        self.assertEqual(smp._restarts, 0)
                        self.assertEqual(chan.hazards, 0)
                        self.assertEqual(chan.stale, 0)
                        # It crossed the ring, so every block boundary was met.
                        self.assertGreater(chan.played, smp.ring_size)

    def test_refreshes_are_a_couple_a_second(self):
        _, chan, _ = _run(seconds=20.0)
        self.assertLessEqual(chan.length_writes, 2 * 20 + 5)
        self.assertGreater(chan.length_writes, 20)

    def test_a_channel_running_ahead_of_its_clock(self):
        # The FPGA's clock runs ahead of the host's.
        smp, chan, _ = _run(ahead=0.02)
        self.assertEqual((smp._restarts, chan.hazards, chan.stale), (0, 0, 0))

    def test_lost_length_writes_are_retried(self):
        # One in twenty register writes lost. The ring was confirmed before
        # the length write went out, so only it is in doubt: it is sent again
        # on the next pass, long before the read head gets near, and no
        # restart skips a lead of audio that landed.
        rng = random.Random(645)
        with self.assertNoLogs("c64cast.audio.sampler", logging.WARNING):
            smp, chan, _ = _run(lose=lambda: rng.random() < 0.05)
        self.assertEqual(smp._restarts, 0)
        self.assertEqual((chan.state, chan.stale, chan.hazards), ("playing", 0, 0))

    def test_a_ring_write_lost_between_refreshes_is_not_played(self):
        # The write went out and was charged lost after it: a refresh that
        # checked only its own write moved the deadline over the gap, and the
        # last lap played there.
        drops = iter([False] * 400 + [True])
        with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
            smp, chan, _ = _run(drop_ring=lambda: next(drops, False))
        self.assertEqual(smp._restarts, 1)
        self.assertEqual((chan.state, chan.stale), ("playing", 0))


class OutageTest(unittest.TestCase):
    def test_the_channel_stops_within_the_lead_and_plays_nothing_stale(self):
        with self.assertLogs("c64cast.audio.sampler", logging.WARNING) as logs:
            smp, chan, seen = _run(seconds=30.0, outage=(12.0, 20.0))
        self.assertIsNotNone(seen["finished_in_outage"])
        self.assertLessEqual(seen["finished_in_outage"], 1.1)
        self.assertEqual(chan.stale, 0)
        # Back once the link is: restarted on the first pass whose writes land.
        self.assertEqual(seen["playing_after"], 0.0)
        self.assertEqual((chan.state, smp._restarts, chan.gates), ("playing", 1, 2))
        # The restart moved the ring's phase to where the channel starts over.
        self.assertLess(seen["offset_error_s"], 0.001)
        self.assertIn("reached its deadline", "\n".join(logs.output))
        # And kept going: the deadline moved on from the restart.
        self.assertGreater(smp._deadline or 0, smp._ring_phase + smp._lead_target)

    def test_without_the_deadline_the_loop_replays_the_last_lap(self):
        # The defect this guards against, on the same model: a lead too
        # shallow for the deadline loops the whole ring through an outage.
        smp, chan, seen = _run(lead=0.3, seconds=30.0, outage=(12.0, 20.0))
        self.assertFalse(smp._uses_deadline)
        self.assertIsNone(seen["finished_in_outage"])
        self.assertGreater(chan.stale, 0)

    def test_an_outage_shorter_than_the_lead_costs_nothing(self):
        with self.assertNoLogs("c64cast.audio.sampler", logging.WARNING):
            smp, chan, seen = _run(seconds=20.0, outage=(10.0, 10.3))
        self.assertIsNone(seen["finished_in_outage"])
        self.assertEqual((smp._restarts, chan.gates, chan.stale), (0, 1, 0))

    def test_a_refresh_landing_after_the_old_deadline_restarts(self):
        # The refresh's flush returns past the old deadline, so it may have
        # landed after the channel stopped there: left standing, a stopped
        # channel would stay silent. The next pass restarts it instead.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._written = old + smp._lead_target
            # A refresh after the first, whose flush confirms what went before.
            smp._ring_mark = chan.write_loss_mark()
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            slow = chan.flush

            def late_flush() -> None:
                clock.now = old / 2 / smp._actual_rate
                slow()

            chan.flush = late_flush  # type: ignore[method-assign]
            smp._advance_deadline(smp._writer_gen)
            self.assertEqual(smp._deadline, old)
            chan.flush = slow  # type: ignore[method-assign]
            with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
                self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual((smp._restarts, chan.gates, chan.state), (1, 2, "playing"))

    def test_a_length_write_landing_after_the_old_deadline_restarts(self):
        # The flush returned in time, but the length write's own send sat in
        # a redial past the old deadline: the voice stopped there before the
        # new one landed, and the next pass restarts it.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._written = old + smp._lead_target
            smp._ring_mark = chan.write_loss_mark()
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            slow = chan.write_regs

            def late_write(base_addr: str, *values: int) -> None:
                clock.now = old / 2 / smp._actual_rate
                slow(base_addr, *values)

            chan.write_regs = late_write  # type: ignore[method-assign]
            smp._advance_deadline(smp._writer_gen)
            chan.write_regs = slow  # type: ignore[method-assign]
            self.assertEqual(smp._deadline, old)
            with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
                self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual((smp._restarts, chan.gates, chan.state), (1, 2, "playing"))

    def test_a_refresh_after_a_counted_ring_loss_sends_nothing(self):
        # Nothing is left to confirm, and on a dead link the flush logs a
        # warning for every refresh until the restart.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._ring_mark = chan.write_loss_mark()
            chan.delivery_epoch += 1
            smp._written = old + smp._lead_target
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            flushes, length_writes = chan.flushes, chan.length_writes
            with self.assertRaisesRegex(s._WritesLost, "lost ring audio"):
                smp._advance_deadline(smp._writer_gen)
            self.assertEqual((chan.flushes, chan.length_writes), (flushes, length_writes))
            self.assertEqual(smp._deadline, old)
            smp.stop()

    def test_a_lost_length_write_is_found_with_nothing_more_to_step_to(self):
        # The ring holds nothing past the unconfirmed deadline, so the refresh
        # due near the confirmed one only flushes; it still finds the loss
        # and holds the deadline where the voice stops.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._ring_mark = chan.write_loss_mark()
            smp._written = old + smp._lead_target
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            sent = chan.write_regs

            def quietly_lost(base_addr: str, *values: int) -> None:
                chan.dropped = True  # charged at the next flush

            chan.write_regs = quietly_lost  # type: ignore[method-assign]
            smp._advance_deadline(smp._writer_gen)
            chan.write_regs = sent  # type: ignore[method-assign]
            new = smp._deadline
            assert new is not None and new > old
            smp._written = new
            clock.now = (old - smp._deadline_confirm_by) / 2 / smp._actual_rate + 0.01
            with self.assertRaisesRegex(s._WritesLost, "deadline write"):
                smp._advance_deadline(smp._writer_gen)
            self.assertEqual(smp._deadline, old)
            smp.stop()

    def test_a_lost_length_write_raises_naming_it(self):
        # The ring was confirmed; only the length write is lost, and the
        # raise names it rather than the ring.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._ring_mark = chan.write_loss_mark()
            smp._written = old + smp._lead_target
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            chan.lose = lambda: True
            with self.assertRaisesRegex(s._WritesLost, "length register"):
                smp._advance_deadline(smp._writer_gen)
            chan.lose = None
            self.assertEqual(smp._deadline, old)
            smp.stop()

    def test_the_next_activation_plays_after_a_stop_lost_to_the_outage(self):
        # The channel stops at its deadline during the outage, and the scene's
        # stop() sends its gate-off over the same dead link. The voice is left
        # gated in `finished`, which only a gate-off leaves, so the next
        # activation's gate-on alone would not start it.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            chan.down = True
            clock.now = 5.0
            chan.advance()
            self.assertEqual(chan.state, "finished")
            smp.stop()
            self.assertEqual(chan.state, "finished")
            chan.down = False
            smp.start(prebuffer_timeout=0.0)
            self.assertEqual(chan.state, "playing")
            smp.stop()

    def test_a_resume_during_a_restart_keeps_the_restored_volume(self):
        # The restart programs the volume it read before its writes went out;
        # a resume's restore landing in between was overwritten with the
        # pause's 0, and nothing would restore it again.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        volume_reg = CTRL + s.REG_VOLUME
        volumes: list[int] = []
        resume: list[threading.Thread] = []
        plain_regs, plain_mem = chan.write_regs, chan.write_memory

        def write_regs(base_addr: str, *values: int) -> None:
            if int(base_addr, 16) == volume_reg:
                if smp._output_silenced and not resume:
                    resume.append(threading.Thread(target=smp.flush))
                    resume[0].start()
                    resume[0].join(0.2)
                volumes.append(values[0])
            plain_regs(base_addr, *values)

        def write_memory(address: str, data_hex: str) -> None:
            if int(address, 16) == volume_reg:
                volumes.append(int(data_hex, 16))
            plain_mem(address, data_hex)

        chan.write_regs = write_regs  # type: ignore[method-assign]
        chan.write_memory = write_memory  # type: ignore[method-assign]
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            smp.flush(silence_output=True)
            clock.now = 5.0
            with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
                self.assertTrue(smp._writer_step(smp._writer_gen))
            resume[0].join(5.0)
            self.assertFalse(resume[0].is_alive())
        self.assertFalse(smp._output_silenced)
        self.assertEqual(volumes[-1], s.SAMPLER_VOLUME_MAX)

    def test_a_plain_splice_does_not_wait_on_the_gate_lock(self):
        # The writer holds _gate_lock across a refresh's flush and a given-up
        # gate-off, a redial each; a seek that writes no volume waited it out,
        # and the wait went into the splice's lateness.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            anchors: list[float] = []
            epoch = smp._flush_epoch
            with smp._gate_lock:
                splice = threading.Thread(target=lambda: anchors.append(smp.flush()))
                splice.start()
                splice.join(1.0)
                blocked = splice.is_alive()
            splice.join(5.0)
            smp.stop()
        self.assertFalse(blocked)
        self.assertEqual(len(anchors), 1)
        self.assertEqual((smp._flush_epoch, smp._cut_epoch), (epoch + 1, epoch + 1))

    def test_a_restart_over_a_slow_link_leaves_the_channel_behind_the_read_head(self):
        # The restart's flush takes 0.3 s to return. A channel running ahead
        # of where the writer thinks it reads reaches the deadline first, and
        # stops with the refresh still taken as on time.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            clock.now = 5.0
            chan.advance()
            self.assertEqual(chan.state, "finished")
            fast = chan.flush

            def slow_flush() -> None:
                fast()
                clock.now += 0.3

            chan.flush = slow_flush  # type: ignore[method-assign]
            with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
                self.assertTrue(smp._writer_step(smp._writer_gen))
            chan.flush = fast  # type: ignore[method-assign]
            chan.advance()
            self.assertEqual(chan.state, "playing")
            # Behind, and only by the gate-on's send: a lag that took in the
            # register flush would play the sound that much behind the picture.
            lag = smp._ring_off(smp._read_consumed_bytes()) - chan.played
            self.assertGreaterEqual(lag, 0)
            self.assertLess(lag, smp._deadline_guard)
            smp.stop()

    def test_a_start_over_a_slow_link_leaves_the_channel_behind_the_read_head(self):
        # As at a restart: a clock read after the gate-on's flush puts the
        # voice ahead of the read head, where it can reach the first deadline
        # unseen.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        fast = chan.flush

        def slow_flush() -> None:
            fast()
            clock.now += 0.3

        chan.flush = slow_flush  # type: ignore[method-assign]
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp._q = cast(Any, _Queue())
            smp.start(prebuffer_timeout=0.0)
            chan.advance()
            self.assertEqual(chan.state, "playing")
            lag = smp._ring_off(smp._read_consumed_bytes()) - chan.played
            self.assertGreaterEqual(lag, 0)
            self.assertLess(lag, smp._deadline_guard)
            smp.stop()


class ForeignLossTest(unittest.TestCase):
    def test_another_threads_losses_are_not_the_writers(self):
        # Every register write sees a loss on some other thread (the render
        # path's), which moves delivery_epoch. Taken as the refresh's own, it
        # held the deadline back until the channel was restarted, over and
        # over, on a link that carried every write the sampler sent.
        with self.assertNoLogs("c64cast.audio.sampler", logging.WARNING):
            smp, chan, _ = _run(foreign=lambda: True)
        self.assertGreater(chan.foreign_losses, 20)
        self.assertEqual((smp._restarts, chan.gates, chan.state, chan.stale), (0, 1, "playing", 0))

    def _finished(self, clock: _Clock) -> tuple[s.UltimateAudioSampler, _Channel]:
        """A started sampler whose channel ran into its first deadline, on
        ``clock`` (the one patched in as the sampler's ``time``)."""
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())
        smp.start(prebuffer_timeout=0.0)
        clock.now = 5.0
        chan.advance()
        self.assertEqual(chan.state, "finished")
        return smp, chan

    def test_the_last_activations_writer_losses_are_not_the_next_ones(self):
        # Each activation's writer is a new thread, whose loss count starts
        # again from zero: a mark kept from the last writer's thread reads as
        # a loss to the next one, and held its first refresh into a restart.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            chan.delivery_epoch = 3
            smp.start(prebuffer_timeout=0.0)
            smp.end_input()
            smp._writer_step(smp._writer_gen)
            smp.stop()
            chan.delivery_epoch = 0
            clock.now = 10.0
            smp.start(prebuffer_timeout=0.0)
            smp.end_input()
            smp._writer_step(smp._writer_gen)
            old = smp._deadline
            assert old is not None
            smp._written = old + smp._lead_target
            clock.now += (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            smp._advance_deadline(smp._writer_gen)
            self.assertGreater(smp._deadline or 0, old)
            smp.stop()
        self.assertEqual(smp._restarts, 0)

    def test_a_restart_counts_only_the_writers_losses(self):
        clock = _Clock()
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp, chan = self._finished(clock)
            chan.foreign = lambda: True
            with self.assertLogs("c64cast.audio.sampler", logging.WARNING):
                self.assertTrue(smp._writer_step(smp._writer_gen))
            chan.foreign = None
            self.assertGreater(chan.foreign_losses, 0)
            self.assertEqual((smp._restarts, chan.state), (1, "playing"))
            smp.stop()

    def test_a_restart_the_link_lost_raises(self):
        clock = _Clock()
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp, chan = self._finished(clock)
            chan.lose = lambda: True
            with self.assertRaisesRegex(s._WritesLost, "lost the channel restart"):
                smp._writer_step(smp._writer_gen)
            chan.lose = None
            self.assertEqual((smp._restarts, chan.state), (0, "finished"))
            smp.stop()

    def test_a_gate_off_counts_only_the_writers_losses(self):
        clock = _Clock()
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp, chan = self._finished(clock)
            chan.foreign = lambda: True
            self.assertTrue(smp._gate_off_landed(smp._writer_gen))
            chan.foreign = None
            self.assertEqual(chan.state, "idle")
            chan.lose = lambda: True
            self.assertFalse(smp._gate_off_landed(smp._writer_gen))
            chan.lose = None
            smp.stop()


class NextDeadlineTest(unittest.TestCase):
    def _sampler(self, rate: int) -> s.UltimateAudioSampler:
        return s.UltimateAudioSampler(
            cast(Any, None), sample_rate=rate, bits=16, ring_base=RING_BASE, ring_size=0x100000
        )

    def test_a_block_crossing_that_mixes_onto_the_reader_stops_at_the_block_end(self):
        # 32 kHz/16-bit with a 1 s lead: the target sits 64 KB past the read
        # head, so its high byte with the old low bytes is where it plays.
        smp = self._sampler(32000)
        consumed = 0x0F000
        old = 0x0FF00
        target = consumed + 0x10000
        self.assertFalse(smp._length_safe(old, target, consumed))
        self.assertEqual(smp._next_deadline(old, target, consumed), 0x0FFFE)

    def test_never_a_deadline_at_offset_zero(self):
        smp = self._sampler(44100)
        new = smp._next_deadline(smp.ring_size - 0x800, smp.ring_size, smp.ring_size - 0x9000)
        self.assertEqual(new, smp.ring_size - smp.bps)
        with self.assertRaises(ValueError):
            smp._deadline_offset(smp.ring_size)


class ShallowLeadTest(unittest.TestCase):
    def test_a_lead_under_eight_guards_loops_the_whole_ring(self):
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=44100, bits=16, lead_seconds=0.3, ring_size=0x10000
        )
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp.start(prebuffer_timeout=0.0)
        self.assertIsNone(smp._deadline)
        self.assertEqual(int.from_bytes(bytes(chan.length), "big"), smp.ring_size)


if __name__ == "__main__":
    unittest.main()
