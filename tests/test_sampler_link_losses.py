"""How the sampler reports and accounts for writes the link lost
(c64cast/audio/sampler.py): the log line names the write that was lost, a
restart in flight cannot undo stop(), and a cut-over blank lost on the
playlist thread counts against the writer's deadline."""

from __future__ import annotations

import logging
import threading
import unittest
from typing import Any, cast
from unittest import mock

from _fakes import quiet_logging
from test_sampler_deadline import RING_BASE, _Channel, _Clock, _Queue, _Writer

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

    def test_a_defect_is_not_called_a_ring_write(self):
        logs = self._loop_once(ValueError("no deadline can sit at ring offset 0"))
        warning = next(m for m in logs if m.startswith("WARNING"))
        self.assertIn("writer step raised ValueError (no deadline", warning)
        self.assertNotIn("ring write failed", warning)

    def test_the_give_up_names_the_last_failure(self):
        smp = s.UltimateAudioSampler(cast(Any, None), sample_rate=8000, bits=16)
        smp._running = True  # a current writer's give-up; no thread runs
        with (
            mock.patch.object(smp, "_gate_off_landed", return_value=None),
            self.assertLogs("c64cast.audio.sampler", logging.ERROR) as logs,
        ):
            smp._give_up(s._WritesLost("the link lost ring audio"), smp._writer_gen)
        self.assertIn("(last: the link lost ring audio)", logs.output[0])
        self.assertNotIn("ring write", logs.output[0])


def _finished(clock: _Clock) -> tuple[s.UltimateAudioSampler, _Channel]:
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
    return smp, chan


class StopDuringRestartTest(unittest.TestCase):
    def _stop_mid_restart(
        self, smp: s.UltimateAudioSampler, chan: _Channel, *, after_stop: Any = None
    ) -> BaseException | None:
        """Run a restart that a slow blank holds until a stop() has run
        whole beside it, then ``after_stop``, then let it go on. Returns
        what the restart raised."""
        in_blank, release = threading.Event(), threading.Event()
        plain_reu = chan.reu_write
        raised: list[BaseException] = []

        def slow_reu(offset: int, data: bytes) -> None:
            in_blank.set()
            release.wait(5.0)
            plain_reu(offset, data)

        def restart_channel() -> None:
            try:
                smp._restart_channel(smp._writer_gen)
            except BaseException as e:
                raised.append(e)

        chan.reu_write = slow_reu  # type: ignore[method-assign]
        restart = threading.Thread(target=restart_channel)
        stop = threading.Thread(target=smp.stop)
        restart.start()
        try:
            self.assertTrue(in_blank.wait(5.0))
            stop.start()
            stop.join(5.0)
            # stop() does not wait for the restart: a given-up writer's
            # gate-off can hold the same lock across a whole dial.
            self.assertFalse(stop.is_alive())
            if after_stop is not None:
                after_stop()
            release.set()
            restart.join(5.0)
        finally:
            release.set()
            restart.join(5.0)
            if stop.ident is not None:
                stop.join(5.0)
        self.assertFalse(restart.is_alive() or stop.is_alive())
        return raised[0] if raised else None

    def test_the_gate_off_lands_after_a_restart_in_flight(self):
        # The writer outlived stop()'s join inside a restart, its blank held
        # up on a slow link. A gate-off sent meanwhile was overtaken by the
        # restart's gate-on, and the channel played on after the stop.
        clock = _Clock()
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp, chan = _finished(clock)
            self.assertEqual(chan.state, "finished")
            self.assertIsNone(self._stop_mid_restart(smp, chan))
        self.assertEqual(chan.state, "idle")
        self.assertFalse(chan.ctrl & s.CTRL_GATE)

    def test_a_gate_on_whose_flush_raises_is_gated_off_too(self):
        # The gate-on went out and its flush then raised (a redial since the
        # last flush): the restart left through the raise, past its check of
        # _running, and the channel played on after the stop.
        clock = _Clock()
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp, chan = _finished(clock)
            plain_flush = chan.flush

            def flush_raising_after_gate_on() -> None:
                plain_flush()
                if chan.ctrl & s.CTRL_GATE:
                    raise ConnectionError("commands sent before the reconnect may be lost")

            def after_stop() -> None:
                chan.flush = flush_raising_after_gate_on  # type: ignore[method-assign]

            raised = self._stop_mid_restart(smp, chan, after_stop=after_stop)
        self.assertIsInstance(raised, ConnectionError)
        self.assertEqual(chan.state, "idle")
        self.assertFalse(chan.ctrl & s.CTRL_GATE)


class _PerThreadChannel(_Channel):
    """`_Channel` whose dropped ring writes are charged, at the next flush,
    to the thread that sent them, as the Ultimate's backend charges them."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.lost_by: dict[int, int] = {}
        self.dropped_by: set[int] = set()

    def write_loss_mark(self) -> int:
        return self.lost_by.get(threading.get_ident(), 0)

    def reu_write(self, offset: int, data: bytes) -> None:
        if self.drop_ring is not None and self.drop_ring():
            self.dropped_by.add(threading.get_ident())
            return
        super().reu_write(offset, data)

    def flush(self) -> None:
        me = threading.get_ident()
        if me in self.dropped_by:
            self.dropped_by.discard(me)
            self.lost_by[me] = self.lost_by.get(me, 0) + 1
        super().flush()


class CutOverBlankTest(unittest.TestCase):
    def _due_refresh(self, *, lose_blank: bool) -> tuple[s.UltimateAudioSampler, int]:
        """A splice flushed on a thread of its own, its blank lost when
        ``lose_blank``, and then a writer with a refresh due. Returns the
        sampler and the deadline before the refresh."""
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _PerThreadChannel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())
        self.addCleanup(smp.stop)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp.start(prebuffer_timeout=0.0)
            old = smp._deadline
            assert old is not None
            smp._written = old + smp._lead_target
            smp._ring_mark = chan.write_loss_mark()
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            playlist = threading.Thread(target=smp.flush)
            chan.drop_ring = (lambda: True) if lose_blank else None
            playlist.start()
            playlist.join(5.0)
            chan.drop_ring = None
            self.assertFalse(playlist.is_alive())
            # The writer has since written past the old deadline.
            smp._written = old + smp._lead_target
        return smp, old

    def test_a_lost_blank_holds_the_deadline(self):
        # The blank went out on the playlist thread and was charged lost
        # there: a refresh that checked only the writer's own losses moved
        # the deadline over pre-splice audio the blank should have cleared.
        smp, old = self._due_refresh(lose_blank=True)
        with mock.patch.object(s, "time", _Clock()) as clock:
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            with self.assertRaisesRegex(ConnectionError, "lost ring audio"):
                smp._advance_deadline(smp._writer_gen)
        self.assertEqual(smp._deadline, old)
        self.assertTrue(smp._cut_over_lost)

    def test_a_landed_blank_lets_the_deadline_move(self):
        smp, old = self._due_refresh(lose_blank=False)
        with mock.patch.object(s, "time", _Clock()) as clock:
            clock.now = (old - smp._deadline_refresh) / 2 / smp._actual_rate + 0.01
            smp._advance_deadline(smp._writer_gen)
        self.assertGreater(smp._deadline or 0, old)
        self.assertFalse(smp._cut_over_lost)

    def test_the_restart_clears_it(self):
        smp, _ = self._due_refresh(lose_blank=True)
        with (
            mock.patch.object(s, "time", _Clock()),
            self.assertLogs("c64cast.audio.sampler", logging.WARNING),
        ):
            self.assertTrue(smp._restart_channel(smp._writer_gen))
        self.assertFalse(smp._cut_over_lost)

    def test_a_channel_with_no_deadline_does_not_flush_the_blank(self):
        # Nothing holds a deadline the channel does not have, so the round
        # trip would only keep the writer off _io_lock.
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, lead_seconds=0.3, ring_base=RING_BASE
        )
        self.assertFalse(smp._uses_deadline)
        chan = _Channel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())
        self.addCleanup(smp.stop)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp.start(prebuffer_timeout=0.0)
            smp._written = smp._lead_target
            before = chan.flushes
            smp.flush()
        self.assertEqual(chan.flushes, before)


class _VolumeChannel(_Channel):
    """`_Channel` that keeps the channel volume register."""

    volume: int | None = None
    lose_volume = False  # lose every live volume write, and nothing else

    def write_memory(self, address: str, data_hex: str) -> None:
        if int(address, 16) == s.channel_base(0) + s.REG_VOLUME:
            self.advance()
            if self.lose_volume:
                self.delivery_epoch += 1
            elif not self._lost():
                self.volume = int(data_hex, 16)
            return
        super().write_memory(address, data_hex)

    def write_regs(self, base_addr: str, *values: int) -> None:
        # A restart programs the volume among the channel's registers.
        if int(base_addr, 16) == s.channel_base(0) + s.REG_VOLUME:
            self.advance()
            if not self._lost():
                self.volume = values[0]
            return
        super().write_regs(base_addr, *values)


class VolumeRestoreTest(unittest.TestCase):
    def _resumed(self, *, lose_restore: bool) -> tuple[s.UltimateAudioSampler, _VolumeChannel]:
        """A sampler paused (volume 0) and resumed, the resume's restore lost
        when ``lose_restore``."""
        clock = _Clock()
        smp = s.UltimateAudioSampler(
            cast(Any, None), sample_rate=8000, bits=16, ring_base=RING_BASE, ring_size=0x30000
        )
        chan = _VolumeChannel(clock, ring=smp.ring_size, byte_rate=smp._actual_rate * 2, bps=2)
        smp.api = cast(Any, chan)
        smp._q = cast(Any, _Queue())

        def quiet_stop() -> None:
            # The writer step pads the empty ring; the report is asserted by
            # test_sampler's test_stop_reports_underruns_once.
            with quiet_logging():
                smp.stop()

        self.addCleanup(quiet_stop)
        with mock.patch.object(s, "time", clock), mock.patch.object(s, "PollThread", _Writer):
            smp.start(prebuffer_timeout=0.0)
            smp.flush(silence_output=True)
            self.assertEqual(chan.volume, 0)
            if lose_restore:
                lost = iter([True])
                chan.lose = lambda: next(lost, False)
                with self.assertLogs("c64cast.audio.sampler", logging.WARNING) as logs:
                    smp.flush()
                self.assertIn("lost the volume restore", logs.output[0])
            else:
                smp.flush()
        return smp, chan

    def test_a_lost_restore_is_sent_again_by_the_writer(self):
        # Taken as restored, a restore the link lost left the channel muted
        # for the rest of the scene: nothing sent the volume again.
        smp, chan = self._resumed(lose_restore=True)
        self.assertEqual(chan.volume, 0)
        with mock.patch.object(s, "time", _Clock()), self.assertLogs("c64cast.audio.sampler"):
            smp._writer_step(smp._writer_gen)
        self.assertEqual(chan.volume, smp._volume)
        self.assertFalse(smp._volume_owed)

    def test_a_landed_restore_owes_nothing(self):
        smp, chan = self._resumed(lose_restore=False)
        self.assertEqual(chan.volume, smp._volume)
        self.assertFalse(smp._volume_owed)

    def test_a_pause_before_the_retry_keeps_the_channel_muted(self):
        smp, chan = self._resumed(lose_restore=True)
        with mock.patch.object(s, "time", _Clock()):
            smp.flush(silence_output=True)
            smp._writer_step(smp._writer_gen)
        self.assertEqual(chan.volume, 0)

    def test_a_restore_lost_again_raises_and_stays_owed(self):
        smp, chan = self._resumed(lose_restore=True)
        chan.lose = lambda: True
        with (
            mock.patch.object(s, "time", _Clock()),
            self.assertRaisesRegex(ConnectionError, "volume restore"),
        ):
            smp._writer_step(smp._writer_gen)
        self.assertTrue(smp._volume_owed)

    def test_a_restart_settles_the_owed_restore(self):
        smp, chan = self._resumed(lose_restore=True)
        self.assertTrue(smp._volume_owed)
        with mock.patch.object(s, "time", chan.clock), self.assertLogs("c64cast.audio.sampler"):
            self.assertTrue(smp._restart_channel(smp._writer_gen))
        self.assertEqual(chan.volume, smp._volume)
        self.assertFalse(smp._volume_owed)

    def test_a_restore_still_lost_does_not_hold_off_the_restart(self):
        # Sent ahead of the deadline check, a restore the link kept losing
        # raised on every pass, and the channel stopped at its deadline.
        smp, chan = self._resumed(lose_restore=True)
        self.assertTrue(smp._volume_owed)
        chan.lose_volume = True
        assert smp._deadline is not None
        chan.clock.now = smp._deadline / (smp._actual_rate * smp.bps) + 1.0
        with mock.patch.object(s, "time", chan.clock), self.assertLogs("c64cast.audio.sampler"):
            self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(chan.volume, smp._volume)
        self.assertFalse(smp._volume_owed)

    def test_a_restore_lost_into_the_guard_restarts_the_channel(self):
        # The restore's flush ran into the guard and then raised: the full
        # back-off it bought stopped the voice before the next pass restarted.
        smp, chan = self._resumed(lose_restore=True)
        chan.lose_volume = True
        assert smp._deadline is not None
        past = smp._deadline / (smp._actual_rate * smp.bps) + 1.0
        plain_write = chan.write_memory

        def slow_write(address: str, data_hex: str) -> None:
            chan.clock.now = past
            plain_write(address, data_hex)

        chan.write_memory = slow_write  # type: ignore[method-assign]
        with (
            mock.patch.object(s, "time", chan.clock),
            self.assertLogs("c64cast.audio.sampler") as logs,
        ):
            self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertIn("volume restore; restarting the channel", "\n".join(logs.output))
        self.assertEqual(chan.volume, smp._volume)
        self.assertFalse(smp._volume_owed)

    def test_arm_clears_the_owed_restore(self):
        smp, _chan = self._resumed(lose_restore=True)
        with quiet_logging():
            smp.stop()
        self.assertTrue(smp._volume_owed)
        smp.arm()
        self.assertFalse(smp._volume_owed)


if __name__ == "__main__":
    unittest.main()
