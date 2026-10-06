"""Tests for the REU mic lead servo (c64cast/audio/mic_lead.py, #560).

The control loop runs against a simulated pump whose rate is off by the drift
measured on hardware (+1.8 KB/s under mhires, -32 B/s under petscii), driven
tick by tick without the thread. The shaper is checked on synthetic tones."""

from __future__ import annotations

import unittest

import numpy as np

from c64cast.audio import mic_lead as ml
from c64cast.audio.audio_handlers import (
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_SIZE,
    REU_PUMP_CHUNK_SIZE,
)

RATE = 12000


class _Rig:
    """A pump advancing at ``RATE - drift`` B/s and a host writing at
    ``RATE * (1 - drop_frac)``; one ``step`` is one servo interval."""

    def __init__(self, drift: float, *, lead: float = REU_MIC_BOOTSTRAP_BYTES) -> None:
        self.drift = drift
        self.pump = 0.0
        self.host = lead
        self.t = 0.0
        self.fail_reads = 0
        self.raise_reads = False
        self.torn = False
        self.reads = 0
        self.timeouts: list[float] = []
        self.servo = ml.MicLeadServo(
            read_memory=self.read,
            write_pos=lambda: int(self.host) % REU_MIC_SIZE,
            sample_rate=RATE,
            clock=lambda: self.t,
        )

    def read(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        assert (address, length) == (REU_AUDIO_SRC_TRACKER_ADDR, 3)
        self.reads += 1
        self.timeouts.append(timeout)
        if self.raise_reads:
            raise RuntimeError("no read capability")
        if self.fail_reads:
            self.fail_reads -= 1
            return None
        pump = int(self.pump) // REU_PUMP_CHUNK_SIZE * REU_PUMP_CHUNK_SIZE
        if self.torn and self.reads % 2 == 0:
            pump += 0x2000  # a carry the pump had not propagated yet
        src = REU_MIC_BASE + pump % REU_MIC_SIZE
        return bytes([src & 0xFF, (src >> 8) & 0xFF, (src >> 16) & 0xFF])

    @property
    def lead(self) -> float:
        return self.host - self.pump

    def step(self) -> None:
        self.servo.tick()
        anchor = self.servo.take_reanchor()
        if anchor is not None:
            self.host = self.pump + REU_MIC_BOOTSTRAP_BYTES
        self.t += 1.0
        self.pump += RATE - self.drift
        self.host += RATE * (1.0 - self.servo.drop_frac)


class MicLeadCorrectionTest(unittest.TestCase):
    def test_on_target_with_no_history_asks_for_nothing(self):
        drop, integ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES, 0.0, sample_rate=RATE)
        self.assertEqual((drop, integ), (0.0, 0.0))

    def test_a_lead_past_target_drops_and_short_of_it_repeats(self):
        ahead, _ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES + 1000, 0.0, sample_rate=RATE)
        behind, _ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES - 1000, 0.0, sample_rate=RATE)
        self.assertGreater(ahead, 0.0)
        self.assertLess(behind, 0.0)

    def test_output_is_clamped_to_the_drop_ceiling_and_the_resample_floor(self):
        hi, _ = ml.mic_lead_correction(10**6, 10**9, sample_rate=RATE)
        lo, _ = ml.mic_lead_correction(-(10**6), -(10**9), sample_rate=RATE)
        self.assertEqual(hi, ml.MIC_LEAD_MAX_DROP)
        self.assertEqual(lo, -ml.MIC_LEAD_RESAMPLE_MAX)

    def test_a_steady_error_accumulates_in_the_integrator(self):
        integ = 0.0
        drops = []
        for _ in range(3):
            drop, integ = ml.mic_lead_correction(
                REU_MIC_BOOTSTRAP_BYTES + 100, integ, sample_rate=RATE
            )
            drops.append(drop)
        self.assertLess(drops[0], drops[1])
        self.assertLess(drops[1], drops[2])

    def test_a_pinned_repeat_does_not_wind_the_integrator_past_it(self):
        # Short of target for a minute, the output sits at the resample floor.
        # An integrator that kept winding past what that floor can express
        # would hold the output there after the lead swung well past target.
        integ = 0.0
        for _ in range(60):
            _, integ = ml.mic_lead_correction(
                REU_MIC_BOOTSTRAP_BYTES - 800, integ, sample_rate=RATE
            )
        drop, _ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES + 2000, integ, sample_rate=RATE)
        self.assertGreater(drop, 0.0)


class MicLeadClosedLoopTest(unittest.TestCase):
    """The loop against the drift measured on a U64 (#560)."""

    def _settle(self, drift: float, steps: int = 120) -> _Rig:
        rig = _Rig(drift)
        leads = []
        for _ in range(steps):
            rig.step()
            leads.append(rig.lead)
        self.assertEqual(rig.servo.reanchors, 0)
        self.assertLess(max(leads), ml.MIC_LEAD_REANCHOR_ABOVE)
        self.assertGreater(min(leads), 0)
        for lead in leads[40:]:
            self.assertLess(abs(lead - REU_MIC_BOOTSTRAP_BYTES), 300)
        return rig

    def test_mhires_drift_is_held_near_the_bootstrap_lead_by_splicing(self):
        rig = self._settle(drift=1800.0)
        self.assertGreater(rig.servo.drop_frac, ml.MIC_LEAD_RESAMPLE_MAX)

    def test_petscii_drift_is_held_by_repeating_within_the_resample_band(self):
        rig = self._settle(drift=-32.0)
        self.assertLess(rig.servo.drop_frac, 0.0)
        self.assertGreater(rig.servo.drop_frac, -ml.MIC_LEAD_RESAMPLE_MAX)

    def test_a_slow_start_does_not_drive_the_lead_into_an_overtake(self):
        # On hardware the pump runs slower for its first seconds under mhires
        # than in steady state. A loop that trusted that first rate (an
        # integrator seeded from it) undershot to 115 B; here it overtakes.
        rig = _Rig(drift=2600.0)
        low = float("inf")
        for i in range(60):
            if i == 3:
                rig.drift = 1700.0
            rig.step()
            low = min(low, rig.lead)
        self.assertEqual(rig.servo.reanchors, 0)
        self.assertGreater(low, 1000)

    def test_without_the_servo_the_same_drift_overtakes(self):
        # The rig itself reproduces the defect when the loop's output is
        # ignored, so the two tests above are about the loop.
        rig = _Rig(drift=-32.0)
        for _ in range(60):
            rig.t += 1.0
            rig.pump += RATE + 32.0
            rig.host += RATE
        self.assertLess(rig.lead, 0)


class MicLeadReanchorTest(unittest.TestCase):
    def test_an_overtaken_head_is_reanchored_ahead_of_the_pump(self):
        rig = _Rig(drift=0.0, lead=-500)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING") as cm:
            rig.servo.tick()
        self.assertIn("overtaken", cm.output[0])
        self.assertEqual(rig.servo.reanchors, 1)
        # Measured at t=0 with the pump at 0; claimed 0.5 s later the estimate
        # has moved on at the pump's rate.
        rig.t = 0.5
        self.assertEqual(rig.servo.take_reanchor(), RATE // 2)
        self.assertIsNone(rig.servo.take_reanchor())

    def test_the_anchor_is_stamped_at_the_middle_of_its_read(self):
        # Each read takes 0.1 s; the second runs from t=0.1 to t=0.2, so the
        # pump position it returned is dated t=0.15, not when it came back.
        rig = _Rig(drift=0.0, lead=-500)

        def slow_read(address: int, length: int, timeout: float = 1.0) -> bytes | None:
            raw = rig.read(address, length, timeout)
            rig.t += 0.1
            return raw

        servo = ml.MicLeadServo(
            read_memory=slow_read,
            write_pos=lambda: int(rig.host) % REU_MIC_SIZE,
            sample_rate=RATE,
            clock=lambda: rig.t,
        )
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            servo.tick()
        rig.t = 0.25
        self.assertEqual(servo.take_reanchor(), RATE // 10)

    def test_a_lead_far_past_target_is_reanchored(self):
        rig = _Rig(drift=0.0, lead=ml.MIC_LEAD_REANCHOR_ABOVE + 1000)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING") as cm:
            rig.servo.tick()
        self.assertIn("too far ahead", cm.output[0])
        self.assertIsNotNone(rig.servo.take_reanchor())

    def test_no_new_measurement_while_a_reanchor_is_unclaimed(self):
        rig = _Rig(drift=0.0, lead=-500)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        reads = rig.reads
        rig.servo.tick()
        self.assertEqual(rig.reads, reads)

    def test_an_unclaimed_reanchor_is_dropped_and_measuring_resumes(self):
        rig = _Rig(drift=0.0, lead=-500)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        reads = rig.reads
        rig.t = ml.MIC_LEAD_REANCHOR_CLAIM_INTERVALS * ml.MIC_LEAD_SERVO_INTERVAL_S
        rig.servo.tick()  # still within the claim window
        self.assertEqual(rig.reads, reads)
        rig.t += 0.5
        rig.pump += RATE // 2
        rig.host = rig.pump + REU_MIC_BOOTSTRAP_BYTES
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING") as cm:
            rig.servo.tick()
        self.assertIn("has not taken a re-anchor", cm.output[0])
        self.assertGreater(rig.reads, reads)
        self.assertIsNone(rig.servo.take_reanchor())

    def test_later_unclaimed_reanchors_drop_below_warning(self):
        rig = _Rig(drift=0.0, lead=-500)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        window = ml.MIC_LEAD_REANCHOR_CLAIM_INTERVALS * ml.MIC_LEAD_SERVO_INTERVAL_S
        for drops in (1, 2):
            rig.t += window + 0.5
            rig.pump += RATE
            with self.assertLogs("c64cast.audio.mic_lead", "DEBUG") as cm:
                rig.servo.tick()
            levels = [r.levelname for r in cm.records if "has not taken" in r.getMessage()]
            self.assertEqual(levels, ["WARNING" if drops == 1 else "DEBUG"])
        self.assertEqual(rig.servo.reanchors_dropped, 2)

    def test_later_reanchors_log_below_warning(self):
        rig = _Rig(drift=0.0, lead=-500)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        rig.servo.take_reanchor()
        rig.t += 1.0
        rig.pump += RATE
        with self.assertLogs("c64cast.audio.mic_lead", "DEBUG") as cm:
            rig.servo.tick()
        self.assertEqual([r.levelname for r in cm.records], ["DEBUG"])
        self.assertEqual(rig.servo.reanchors, 2)


class MicLeadReanchorReseedTest(unittest.TestCase):
    """AUD-7 A-F1: a re-anchor resets the lead but not the rate mismatch, so it
    restarts the loop from the pump's measured rate. Left at the pre-jump drop,
    a pump that sped up mid-scene kept being over-dropped from: the lead fell
    through zero again within seconds, and the loop re-anchored every couple of
    seconds, each one a NEUTRAL dropout."""

    def test_the_seed_matches_the_host_to_the_pump_rate(self):
        drop, integ = ml.mic_lead_rate_seed(RATE * 0.85, sample_rate=RATE)
        self.assertAlmostEqual(drop, 0.15)
        # With the lead back on target the proportional term is zero, so the
        # integrator alone has to carry the drop.
        held, _ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES, integ, sample_rate=RATE)
        self.assertAlmostEqual(held, 0.15)

    def test_the_seed_is_clamped_to_the_output_range(self):
        self.assertEqual(
            ml.mic_lead_rate_seed(RATE * 2.0, sample_rate=RATE)[0], -ml.MIC_LEAD_RESAMPLE_MAX
        )
        self.assertEqual(
            ml.mic_lead_rate_seed(RATE * 0.25, sample_rate=RATE)[0], ml.MIC_LEAD_MAX_DROP
        )

    def test_a_rate_that_says_nothing_about_the_pump_seeds_the_startup_state(self):
        # NaN slipped through the clamp as the full 35 % drop, the side that
        # overtakes; a stopped or nonsense rate is no basis for a drop at all.
        for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -RATE):
            with self.subTest(pump_rate=bad):
                self.assertEqual(ml.mic_lead_rate_seed(bad, sample_rate=RATE), (0.0, 0.0))

    def test_a_pump_that_speeds_up_mid_scene_does_not_cycle_through_reanchors(self):
        # Settled under the mhires deficit, then the pump comes up to within
        # the petscii drift of the host. The lead falls through zero within the
        # first interval, once; unseeded, the stale 15 % drop re-anchored on
        # every tick, and seeded from the rate before that interval (the same
        # 15 %) it overtook twice more while the rate average caught up.
        rig = _Rig(drift=1800.0)
        for _ in range(60):
            rig.step()
        self.assertEqual(rig.servo.reanchors, 0)
        rig.drift = 32.0
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            leads = []
            seeded = None
            for _ in range(60):
                rig.step()
                leads.append(rig.lead)
                if seeded is None and rig.servo.reanchors == 1:
                    seeded = rig.servo.drop_frac
        self.assertEqual(rig.servo.reanchors, 1)
        # The overtaking interval's own rate, not the 15 % before it.
        assert seeded is not None
        self.assertAlmostEqual(seeded, 32.0 / RATE, delta=0.015)
        for lead in leads[-20:]:
            self.assertGreater(lead, 0)
            self.assertLess(abs(lead - REU_MIC_BOOTSTRAP_BYTES), 300)

    def test_a_stall_that_forces_the_reanchor_does_not_set_its_seed(self):
        # The pump halts for one interval, so the lead jumps past the re-anchor
        # threshold. That interval reads the pump as stopped; the seed comes
        # from the rate before it, the ~15 % deficit the pump resumes at.
        rig = _Rig(drift=1800.0)
        for _ in range(60):
            rig.step()
        rig.t += 1.0
        rig.host += RATE * (1.0 - rig.servo.drop_frac)  # the pump does not move
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        self.assertEqual(rig.servo.reanchors, 1)
        self.assertAlmostEqual(rig.servo.drop_frac, 1800.0 / RATE, delta=0.01)

    def test_a_stall_that_crosses_a_tick_does_not_set_its_seed_either(self):
        # The pump halts for the second half of one interval and the first half
        # of the next. The first tick reads it at half speed and steers; the lap
        # comes at the second, by which time the rate average has taken in the
        # first half. Seeded from that average the loop over-drops and overtakes
        # within the next interval, a second NEUTRAL dropout.
        rig = _Rig(drift=1800.0)
        for _ in range(60):
            rig.step()
        rig.servo.tick()

        def half_stalled_interval() -> None:
            rig.t += 1.0
            rig.pump += (RATE - rig.drift) / 2
            rig.host += RATE * (1.0 - rig.servo.drop_frac)

        half_stalled_interval()
        rig.servo.tick()
        self.assertEqual(rig.servo.reanchors, 0)
        half_stalled_interval()
        self.assertGreater(rig.lead, ml.MIC_LEAD_REANCHOR_ABOVE)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        self.assertEqual(rig.servo.reanchors, 1)
        # The tick that steered on the first half folded its error into the
        # integrator: one tick's worth, short of the lap limit, on top of 15 %.
        one_tick = ml.MIC_LEAD_KI * ml.MIC_LEAD_REANCHOR_ABOVE / RATE
        self.assertGreater(rig.servo.drop_frac, 1800.0 / RATE - 0.01)
        self.assertLess(rig.servo.drop_frac, 1800.0 / RATE + one_tick)
        rig.servo.take_reanchor()
        rig.host = rig.pump + REU_MIC_BOOTSTRAP_BYTES
        rig.t += 1.0
        rig.pump += RATE - rig.drift
        rig.host += RATE * (1.0 - rig.servo.drop_frac)
        for _ in range(30):
            rig.step()
        self.assertEqual(rig.servo.reanchors, 1)

    def test_a_crawl_that_laps_for_several_ticks_does_not_set_its_seed(self):
        # The pump crawls at a tenth of its rate for three intervals, lapping
        # at every tick. Each lapping interval reads the pump as slow; a seed
        # taken from a rate average, however far back it reaches, is pulled
        # down by the third, over-drops, and overtakes once the pump resumes.
        rig = _Rig(drift=1800.0)
        for _ in range(60):
            rig.step()
        rig.drift = RATE - 0.1 * (RATE - 1800.0)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            for _ in range(3):
                rig.step()
            rig.drift = 1800.0
            rig.step()  # its tick laps on the third crawling interval
        crawled = rig.servo.reanchors
        self.assertEqual(crawled, 3)
        self.assertAlmostEqual(rig.servo.drop_frac, 1800.0 / RATE, delta=0.01)
        for _ in range(30):
            rig.step()
        self.assertEqual(rig.servo.reanchors, crawled)

    def test_a_stall_soon_after_an_absorbed_speed_up_does_not_seed_the_old_drop(self):
        # Settled under the mhires deficit, the pump speeds up to a 6.7 %
        # deficit: too little to overtake, so the loop steers through it while
        # the integrator still holds most of the old 15 %. A stall laps a tick
        # later. Seeded from the integrator alone, the loop puts the stale
        # 15 % back and overtakes once the pump resumes, a second dropout.
        rig = _Rig(drift=1800.0)
        for _ in range(60):
            rig.step()
        rig.drift = 800.0
        rig.step()
        self.assertEqual(rig.servo.reanchors, 0)
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
            rig.t += 1.0
            rig.pump += 0.1 * (RATE - rig.drift)
            rig.host += RATE * (1.0 - rig.servo.drop_frac)
            rig.step()
        self.assertEqual(rig.servo.reanchors, 1)
        self.assertLess(rig.servo.drop_frac, 1800.0 / RATE - 0.02)
        for _ in range(30):
            rig.step()
        self.assertEqual(rig.servo.reanchors, 1)


class MicLeadOpenLoopTest(unittest.TestCase):
    def _closed(self, drift: float = 1800.0) -> _Rig:
        rig = _Rig(drift)
        for _ in range(5):
            rig.step()
        self.assertGreater(rig.servo.drop_frac, 0.0)
        return rig

    def test_repeated_read_failures_open_the_loop_with_a_warning(self):
        rig = self._closed()
        rig.fail_reads = 100
        rig.servo.tick()
        rig.servo.tick()
        self.assertGreater(rig.servo.drop_frac, 0.0)  # one bad read is not an outage
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING") as cm:
            rig.servo.tick()
        self.assertIn("open-loop", cm.output[0])
        self.assertEqual(rig.servo.drop_frac, 0.0)
        self.assertEqual(rig.servo.open_loop_spells, 1)

    def test_a_backend_that_raises_on_read_degrades_the_same_way(self):
        rig = self._closed()
        rig.raise_reads = True
        rig.servo.tick()
        rig.servo.tick()
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        self.assertEqual(rig.servo.drop_frac, 0.0)

    def test_a_failed_measurement_forgets_the_last_pump_position(self):
        rig = self._closed()
        last = rig.servo._last_pump
        assert last is not None
        rig.fail_reads = 1  # one measurement: a failed first read skips the second
        rig.servo.tick()
        # An outage long enough for the pump to go once round the ring: the
        # tracker reads the same offset, and measured against the pre-outage
        # one it would look like an idle pump.
        rig.t += 5.0
        rig.pump = last[0] + REU_MIC_SIZE
        rig.host = rig.pump + REU_MIC_BOOTSTRAP_BYTES
        rig.servo.tick()
        self.assertGreater(rig.servo.drop_frac, 0.0)

    def test_recovered_reads_close_the_loop_again(self):
        rig = self._closed()
        rig.fail_reads = 6
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            for _ in range(3):
                rig.servo.tick()
        with self.assertLogs("c64cast.audio.mic_lead", "INFO") as cm:
            for _ in range(5):
                rig.step()
        self.assertTrue(any("recovered" in line for line in cm.output))
        self.assertGreater(rig.servo.drop_frac, 0.0)

    def test_later_open_loop_spells_log_below_warning(self):
        rig = self._closed()
        rig.fail_reads = 3
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            for _ in range(3):
                rig.servo.tick()
            rig.step()
        rig.fail_reads = 3
        with self.assertLogs("c64cast.audio.mic_lead", "DEBUG") as cm:
            for _ in range(3):
                rig.servo.tick()
            rig.step()
        self.assertEqual(rig.servo.open_loop_spells, 2)
        self.assertNotIn("WARNING", [r.levelname for r in cm.records])
        self.assertTrue(any("open-loop" in r.getMessage() for r in cm.records))
        recovered = [r.levelname for r in cm.records if "recovered" in r.getMessage()]
        self.assertEqual(recovered, ["DEBUG"])

    def test_a_failed_first_read_skips_the_second(self):
        rig = self._closed()
        rig.fail_reads = 1
        reads = rig.reads
        rig.servo.tick()
        self.assertEqual(rig.reads - reads, 1)
        self.assertEqual(rig.servo._fails, 1)

    def test_a_torn_read_pair_is_not_used(self):
        rig = self._closed()
        rig.torn = True
        drop = rig.servo.drop_frac
        rig.servo.tick()
        self.assertEqual(rig.servo.drop_frac, drop)
        self.assertEqual(rig.servo._fails, 1)

    def test_an_idle_pump_is_not_steered(self):
        rig = self._closed()
        rig.servo.tick()
        rig.servo.tick()  # the pump has not moved since the last tick
        self.assertEqual(rig.servo.drop_frac, 0.0)

    def test_a_tracker_outside_the_mic_ring_is_a_failed_read(self):
        servo = ml.MicLeadServo(
            read_memory=lambda a, n, timeout=1.0: bytes([0, 0, 0x20]),
            write_pos=lambda: 0,
            sample_rate=RATE,
        )
        servo.tick()
        self.assertEqual(servo._fails, 1)


class MicLeadThreadTest(unittest.TestCase):
    def test_stop_joins_the_thread(self):
        rig = _Rig(drift=0.0)
        servo = ml.MicLeadServo(
            read_memory=rig.read,
            write_pos=lambda: 0,
            sample_rate=RATE,
            interval_s=0.001,
        )
        servo.start()
        thread = servo._thread
        assert thread is not None
        servo.stop()
        self.assertFalse(thread.is_alive())

    def test_stop_during_the_first_read_skips_the_second(self):
        rig = _Rig(drift=0.0)

        def read(address: int, length: int, timeout: float = 1.0) -> bytes | None:
            rig.servo._stop.set()
            return rig.read(address, length, timeout)

        rig.servo._read = read
        rig.servo.tick()
        self.assertEqual(rig.reads, 1)
        self.assertEqual(rig.servo._fails, 0)  # a stop is not a failed read

    def test_an_open_loop_backs_off_to_a_ceiling(self):
        servo = ml.MicLeadServo(
            read_memory=lambda a, n, timeout=1.0: None,
            write_pos=lambda: 0,
            sample_rate=RATE,
        )
        waits: list[float] = []

        class _Stop:
            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                return len(waits) > 10

            def is_set(self) -> bool:
                return False

        servo._stop = _Stop()  # type: ignore[assignment]
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            servo._run()
        interval = ml.MIC_LEAD_SERVO_INTERVAL_S
        self.assertEqual(waits[:3], [interval] * 3)  # closed through 3 failures
        self.assertEqual(waits[3:6], [2 * interval, 4 * interval, 8 * interval])
        self.assertEqual(max(waits), ml.MIC_LEAD_OPEN_LOOP_MAX_WAIT_S)
        self.assertEqual(waits[-1], ml.MIC_LEAD_OPEN_LOOP_MAX_WAIT_S)

    def test_a_failing_step_opens_the_loop_and_says_so(self):
        def boom() -> int:
            raise ValueError("bug")

        servo = ml.MicLeadServo(
            read_memory=lambda a, n, timeout=1.0: None,
            write_pos=boom,
            sample_rate=RATE,
            interval_s=0.001,
        )
        servo.drop_frac = 0.2
        with self.assertLogs("c64cast.audio.mic_lead", "ERROR") as cm:
            servo.start()
            assert servo._thread is not None
            servo._thread.join(timeout=2.0)
        servo.stop()
        self.assertIn("open-loop", cm.output[0])
        self.assertEqual(servo.drop_frac, 0.0)


def _tone(n: int, freq: float = 440.0, start: int = 0) -> np.ndarray:
    return (0.5 * np.sin(2 * np.pi * freq * (np.arange(n) + start) / RATE)).astype(np.float32)


def _run_shaper(shaper: ml.MicLeadShaper, x: np.ndarray, drop: float) -> np.ndarray:
    return np.concatenate([shaper.process(x[i : i + 256], drop) for i in range(0, len(x), 256)])


class MicLeadShaperTest(unittest.TestCase):
    def test_zero_drop_passes_samples_through_one_sample_late(self):
        x = _tone(4096)
        y = _run_shaper(ml.MicLeadShaper(RATE), x, 0.0)
        self.assertEqual(len(y), len(x))
        np.testing.assert_allclose(y[1:], x[:-1], atol=1e-6)

    def test_a_small_drop_resamples_without_splicing(self):
        shaper = ml.MicLeadShaper(RATE)
        y = _run_shaper(shaper, _tone(RATE * 2), 0.02)
        self.assertAlmostEqual(len(y) / (RATE * 2), 0.98, delta=0.001)
        self.assertEqual(shaper.splices, 0)

    def test_a_negative_drop_repeats(self):
        y = _run_shaper(ml.MicLeadShaper(RATE), _tone(RATE * 2), -0.02)
        self.assertAlmostEqual(len(y) / (RATE * 2), 1.02, delta=0.001)

    def test_a_drop_past_the_resample_band_splices_and_keeps_the_pitch(self):
        shaper = ml.MicLeadShaper(RATE)
        y = _run_shaper(shaper, _tone(RATE * 4), 0.15)
        self.assertAlmostEqual(len(y) / (RATE * 4), 0.85, delta=0.02)
        self.assertGreater(shaper.splices, 0)
        # No resampling in this mode: the waveform is the tone at its own
        # pitch, so its dominant frequency is unchanged ...
        spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        peak = np.fft.rfftfreq(len(y), 1 / RATE)[np.argmax(spec)]
        self.assertAlmostEqual(peak, 440.0, delta=1.0)
        # ... and the matched cuts leave no step bigger than the tone's own.
        max_step = 0.5 * 2 * np.pi * 440 / RATE
        self.assertLess(np.abs(np.diff(y)).max(), 1.05 * max_step)

    def test_splices_are_evenly_spaced(self):
        shaper = ml.MicLeadShaper(RATE)
        counts = []
        x = _tone(RATE * 4)
        for i in range(0, len(x), RATE // 2):
            before = shaper.splices
            for j in range(i, i + RATE // 2, 256):
                shaper.process(x[j : j + 256], 0.15)
            counts.append(shaper.splices - before)
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_splice_mode_needs_the_drop_to_fall_below_the_exit_to_end(self):
        shaper = ml.MicLeadShaper(RATE)
        shaper.process(_tone(256), ml.MIC_LEAD_RESAMPLE_MAX + 0.01)
        self.assertTrue(shaper.splicing)
        shaper.process(_tone(256), (ml.MIC_LEAD_RESAMPLE_MAX + ml.MIC_LEAD_SPLICE_EXIT) / 2)
        self.assertTrue(shaper.splicing)
        shaper.process(_tone(256), ml.MIC_LEAD_SPLICE_EXIT / 2)
        self.assertFalse(shaper.splicing)

    def test_leaving_splice_mode_releases_held_samples(self):
        shaper = ml.MicLeadShaper(RATE)
        # A debt past one splice with too little input to search holds it.
        held_out = sum(len(shaper.process(_tone(256), 0.9)) for _ in range(2))
        self.assertLess(held_out, 512)
        released = len(shaper.process(_tone(256), 0.0))
        # Every input sample comes out (the resampler's one-sample delay
        # emits its seed sample first): nothing stays held.
        self.assertEqual(held_out + released, 3 * 256)


class BestSpliceCutTest(unittest.TestCase):
    def test_the_cut_lands_on_a_whole_period(self):
        period = 40  # 300 Hz at 12 kHz
        x = np.sin(2 * np.pi * np.arange(1000) / period).astype(np.float32)
        cut = ml.best_splice_cut(x, 64, 230, 290)
        self.assertEqual(cut % period, 0)

    def test_silence_takes_the_middle_of_the_range(self):
        self.assertEqual(ml.best_splice_cut(np.zeros(400, np.float32), 32, 100, 200), 150)


class MicLeadRingWrapTest(unittest.TestCase):
    """Offsets near either end of the 64 KB mic ring. Every other test runs
    mid-ring, where a lost ``% REU_MIC_SIZE`` changes nothing."""

    def test_a_fill_short_of_the_ring_start_wraps_to_its_end(self):
        # Unwrapped, the fill starts below REU_MIC_BASE: an REUWRITE outside
        # the ring, and the pump plays the stale bytes the fill was for.
        self.assertEqual(
            ml.reanchor_fill(100),
            (
                REU_MIC_SIZE + 100 - ml.MIC_LEAD_REANCHOR_GUARD,
                REU_MIC_BOOTSTRAP_BYTES + ml.MIC_LEAD_REANCHOR_GUARD,
            ),
        )

    def test_an_anchor_extrapolated_past_the_ring_end_wraps(self):
        rig = _Rig(drift=0.0)
        rig.pump = REU_MIC_SIZE - 1024
        rig.host = rig.pump - 500
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        rig.t = 0.5
        self.assertEqual(rig.servo.take_reanchor(), REU_MIC_SIZE - 1024 + RATE // 2 - REU_MIC_SIZE)

    def test_the_host_midpoint_across_the_ring_end_is_at_the_end(self):
        servo = _Rig(drift=0.0).servo
        self.assertEqual(servo._host_between(REU_MIC_SIZE - 100, 100), 0)


class MicLeadReadGuardTest(unittest.TestCase):
    """A tracker read that comes back unusable is a failed measurement; it
    never raises out of tick(), which would end the servo thread for good."""

    def _servo(self, replies: list[bytes | None]) -> ml.MicLeadServo:
        it = iter(replies)
        return ml.MicLeadServo(
            read_memory=lambda a, n, timeout=1.0: next(it),
            write_pos=lambda: REU_MIC_BOOTSTRAP_BYTES,
            sample_rate=RATE,
        )

    @staticmethod
    def _src(offset: int) -> bytes:
        src = REU_MIC_BASE + offset
        return bytes([src & 0xFF, (src >> 8) & 0xFF, (src >> 16) & 0xFF])

    def test_a_failed_second_read_is_a_failed_measurement(self):
        servo = self._servo([self._src(0), None])
        servo.tick()
        self.assertEqual(servo._fails, 1)

    def test_a_short_read_is_a_failed_measurement(self):
        servo = self._servo([self._src(0)[:2]])
        servo.tick()
        self.assertEqual(servo._fails, 1)

    def test_a_tracker_below_the_mic_ring_is_a_failed_read(self):
        servo = self._servo([self._src(-1)] * 2)
        servo.tick()
        self.assertEqual(servo._fails, 1)

    def test_each_read_carries_the_servo_timeout(self):
        # The join waits about two reads' worth; a backend left at its own
        # default timeout can hold stop() past it.
        rig = _Rig(drift=0.0)
        rig.servo.tick()
        self.assertEqual(rig.timeouts, [ml.MIC_LEAD_READ_TIMEOUT_S] * 2)


class MicLeadRateScalingTest(unittest.TestCase):
    def test_the_same_lead_in_seconds_asks_for_the_same_drop_at_any_rate(self):
        # Gains are per second of lead, so a rate other than the 12 kHz every
        # closed-loop test uses must not change how hard the loop pulls.
        at_12k = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES + 1200, 0.0, sample_rate=12000)
        at_24k = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES + 2400, 0.0, sample_rate=24000)
        self.assertAlmostEqual(at_12k[0], at_24k[0])
        self.assertGreater(at_12k[0], 0.0)

    def test_a_pinned_drop_does_not_wind_the_integrator_past_it(self):
        # The mirror of the pinned-repeat test: far past target for a minute,
        # the output sits at the drop ceiling. Once the lead falls short of
        # target the drop has to come off that ceiling at once.
        integ = 0.0
        for _ in range(60):
            _, integ = ml.mic_lead_correction(
                REU_MIC_BOOTSTRAP_BYTES + 5000, integ, sample_rate=RATE
            )
        drop, _ = ml.mic_lead_correction(REU_MIC_BOOTSTRAP_BYTES - 2000, integ, sample_rate=RATE)
        self.assertLess(drop, ml.MIC_LEAD_MAX_DROP)

    def test_a_reanchor_extrapolates_at_the_averaged_pump_rate(self):
        # The pump's rate is an average of what it starts at (sample_rate) and
        # each measured interval, half and half; the anchor moves on at it.
        rig = _Rig(drift=0.0)
        rig.servo.tick()
        rig.t = 1.0
        rig.pump = 10240.0
        rig.host = rig.pump - 500
        with self.assertLogs("c64cast.audio.mic_lead", "WARNING"):
            rig.servo.tick()
        rig.t = 1.5
        averaged = RATE + 0.5 * (10240 - RATE)
        self.assertEqual(rig.servo.take_reanchor(), 10240 + round(0.5 * averaged))


class MicLeadTelemetryTest(unittest.TestCase):
    def test_lead_min_and_max_span_every_measurement(self):
        servo = _Rig(drift=0.0).servo
        script = iter([(1600, 0, 0.0), (1200, 12000, 1.0), (2000, 24000, 2.0), (1500, 36000, 3.0)])
        servo._measure = lambda: next(script)  # type: ignore[method-assign]
        for _ in range(4):
            servo.tick()
        self.assertEqual((servo.lead_min, servo.lead_max), (1200, 2000))

    def test_skipped_samples_count_what_the_splices_cut(self):
        shaper = ml.MicLeadShaper(RATE)
        x = _tone(RATE * 2)
        y = _run_shaper(shaper, x, 0.15)
        self.assertGreater(shaper.splices, 0)
        # Splice mode resamples nothing, so every input sample is out, held,
        # or cut.
        self.assertEqual(shaper.skipped_samples, len(x) - len(y) - len(shaper._held))


class MicLeadCrossfadeTest(unittest.TestCase):
    def test_a_splice_fades_linearly_from_the_head_into_the_landing(self):
        shaper = ml.MicLeadShaper(RATE)
        buf = np.random.default_rng(7).uniform(-0.5, 0.5, 2000).astype(np.float32)
        shaper._splice_debt = float(shaper.splice_len)
        out = shaper._splice(buf)
        fade = shaper.fade
        cut = ml.best_splice_cut(buf, fade, shaper.cut_min, shaper.cut_max)
        ramp = np.linspace(0.0, 1.0, fade)
        expected = buf[:fade] * (1.0 - ramp) + buf[cut : cut + fade] * ramp
        self.assertEqual(shaper.splices, 1)
        np.testing.assert_allclose(out[:fade], expected, atol=1e-6)
        np.testing.assert_array_equal(out[fade:], buf[cut + fade :])


class MicLeadThreadExitTest(unittest.TestCase):
    def _waits_until_stopped(self, servo: ml.MicLeadServo) -> list[float]:
        waits: list[float] = []

        class _Stop:
            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                return len(waits) > 10

            def is_set(self) -> bool:
                return False

        servo._stop = _Stop()  # type: ignore[assignment]
        servo._run()
        return waits

    def test_a_failing_step_ends_the_loop_after_one_report(self):
        def boom() -> int:
            raise ValueError("bug")

        servo = ml.MicLeadServo(
            read_memory=lambda a, n, timeout=1.0: None, write_pos=boom, sample_rate=RATE
        )
        with self.assertLogs("c64cast.audio.mic_lead", "ERROR") as cm:
            waits = self._waits_until_stopped(servo)
        self.assertEqual(len(waits), 1)
        self.assertEqual(len(cm.records), 1)

    def test_an_open_loop_held_for_hours_still_waits_the_ceiling(self):
        # Without the doubling cap, 2.0 ** fails overflows a float after ~1000
        # failed measurements, raising out of _next_wait in the thread.
        servo = _Rig(drift=0.0).servo
        servo._open_loop = True
        servo._fails = 5000
        self.assertEqual(servo._next_wait(), ml.MIC_LEAD_OPEN_LOOP_MAX_WAIT_S)


if __name__ == "__main__":
    unittest.main()
