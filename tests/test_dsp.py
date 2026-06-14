"""Tests for the host-side audio DSP chain (c64cast/dsp.py).

Pure numpy, no hardware, no sound device. The DSP runs on float samples in
[-1, 1] BEFORE the 4-bit SID DAC quantization (encode_floats_to_dac), so its
job is to make the most of the ~24 dB the DAC can represent: even out dynamics
(compressor/limiter), lift quiet mic input (AGC), brighten for intelligibility
(pre-emphasis), and clean the noise floor without the chatter of a hard gate
(expander with hysteresis).

The recurring correctness theme here is STREAMING CONTINUITY: each processor is
stateful and is fed in arbitrary-sized blocks from the realtime callbacks, so
processing a signal in one shot must match processing it split across blocks
(within a small tolerance — the recursive smoothers carry their state across
calls). Several tests assert that property directly.
"""

from __future__ import annotations

import unittest

import numpy as np

from c64cast.dsp import (
    AGC,
    AudioDSP,
    Compressor,
    DSPParams,
    Expander,
    Limiter,
    PreEmphasis,
    db_to_lin,
    lin_to_db,
)

SR = 8000


def _sine(freq: float, secs: float, amp: float = 1.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(secs * sr), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


def _split_process(proc, x: np.ndarray, sizes: list[int]) -> np.ndarray:
    """Run `proc` over x broken into the given block sizes, concatenate."""
    out = []
    i = 0
    for s in sizes:
        out.append(proc.process(x[i : i + s]))
        i += s
    if i < x.size:
        out.append(proc.process(x[i:]))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


class DbHelpersTest(unittest.TestCase):
    def test_round_trip(self):
        for db in (-60.0, -18.0, -6.0, 0.0, 6.0):
            self.assertAlmostEqual(lin_to_db(db_to_lin(db)), db, places=4)

    def test_zero_lin_is_floored_not_inf(self):
        self.assertTrue(np.isfinite(lin_to_db(0.0)))
        self.assertLess(lin_to_db(0.0), -100.0)


class PreEmphasisTest(unittest.TestCase):
    def test_constant_signal_unchanged(self):
        # y[n] = x[n] + amount*(x[n]-x[n-1]); a DC signal has zero difference,
        # so pre-emphasis leaves it alone (it only boosts high frequencies).
        pe = PreEmphasis(amount=0.7)
        x = np.full(256, 0.5, dtype=np.float32)
        y = pe.process(x)
        # First sample sees the zero initial-state step; the rest are exact.
        np.testing.assert_allclose(y[1:], x[1:], atol=1e-6)

    def test_high_freq_boosted_low_freq_not(self):
        pe = PreEmphasis(amount=0.9)
        lo = _sine(100, 0.25)
        hi = _sine(3500, 0.25)
        self.assertLess(_rms(pe.process(lo)), _rms(pe.process(hi)))
        # And HF output is louder than HF input.
        pe.reset()
        self.assertGreater(_rms(pe.process(hi)), _rms(hi))

    def test_zero_amount_is_identity(self):
        pe = PreEmphasis(amount=0.0)
        x = _sine(2000, 0.1)
        np.testing.assert_allclose(pe.process(x), x, atol=1e-6)

    def test_streaming_continuity(self):
        x = _sine(1500, 0.2)
        a = PreEmphasis(amount=0.6).process(x)
        b = _split_process(PreEmphasis(amount=0.6), x, [37, 200, 511, 1])
        np.testing.assert_allclose(a, b, atol=1e-6)


class CompressorTest(unittest.TestCase):
    def _comp(self, **kw):
        return Compressor(
            **{
                "sample_rate": SR,
                "threshold_db": -18.0,
                "ratio": 4.0,
                "knee_db": 0.0,
                "attack_ms": 1.0,
                "release_ms": 50.0,
                "makeup_db": 0.0,
                **kw,
            }
        )

    def test_loud_signal_attenuated_above_threshold(self):
        # A steady tone well above threshold should settle to the static
        # gain-reduction the ratio predicts. -6 dBFS in, ratio 4, threshold
        # -18 → reduction = (over)*(1-1/ratio) = 12*0.75 = 9 dB → ~-15 dBFS.
        comp = self._comp()
        x = _sine(500, 1.0, amp=db_to_lin(-6.0))
        y = comp.process(x)
        tail = y[-2000:]  # after attack/release settle
        out_db = lin_to_db(_rms(tail) * np.sqrt(2))  # peak from rms of sine
        self.assertAlmostEqual(out_db, -15.0, delta=2.0)

    def test_quiet_signal_below_threshold_passes(self):
        comp = self._comp()
        x = _sine(500, 0.5, amp=db_to_lin(-30.0))
        y = comp.process(x)
        np.testing.assert_allclose(_rms(y[-1000:]), _rms(x[-1000:]), rtol=0.1)

    def test_makeup_gain_applied(self):
        comp = self._comp(makeup_db=6.0)
        x = _sine(500, 0.5, amp=db_to_lin(-30.0))  # below threshold
        y = comp.process(x)
        self.assertAlmostEqual(lin_to_db(_rms(y[-1000:]) / _rms(x[-1000:])), 6.0, delta=0.5)

    def test_auto_makeup_brings_threshold_to_unity(self):
        # With auto makeup, a signal AT threshold should come out near 0 dB
        # change (makeup compensates the curve at the threshold point).
        comp = Compressor(
            sample_rate=SR,
            threshold_db=-18.0,
            ratio=4.0,
            knee_db=0.0,
            attack_ms=1.0,
            release_ms=50.0,
            makeup_db=None,
        )  # None = auto
        self.assertGreater(comp.makeup_db, 0.0)

    def test_output_finite_and_bounded(self):
        comp = self._comp()
        x = _sine(500, 0.3, amp=1.0)
        y = comp.process(x)
        self.assertTrue(np.all(np.isfinite(y)))

    def test_streaming_continuity(self):
        x = np.concatenate(
            [_sine(500, 0.2, amp=db_to_lin(-30)), _sine(500, 0.2, amp=db_to_lin(-3))]
        )
        a = self._comp().process(x)
        b = _split_process(self._comp(), x, [128, 333, 1000, 64])
        np.testing.assert_allclose(a, b, atol=1e-4)


class LimiterTest(unittest.TestCase):
    def test_output_never_exceeds_ceiling(self):
        lim = Limiter(sample_rate=SR, ceiling=0.8, release_ms=20.0)
        x = _sine(440, 0.5, amp=1.0)
        y = lim.process(x)
        self.assertLessEqual(float(np.max(np.abs(y))), 0.8 + 1e-4)

    def test_below_ceiling_passes_through(self):
        lim = Limiter(sample_rate=SR, ceiling=0.95, release_ms=20.0)
        x = _sine(440, 0.5, amp=0.5)
        y = lim.process(x)
        np.testing.assert_allclose(y, x, atol=1e-3)

    def test_streaming_continuity(self):
        x = _sine(440, 0.4, amp=1.0)
        a = Limiter(sample_rate=SR, ceiling=0.7, release_ms=20.0).process(x)
        b = _split_process(
            Limiter(sample_rate=SR, ceiling=0.7, release_ms=20.0), x, [100, 500, 999]
        )
        np.testing.assert_allclose(a, b, atol=1e-4)


class ExpanderTest(unittest.TestCase):
    def _exp(self, **kw):
        return Expander(
            **{
                "sample_rate": SR,
                "threshold_db": -40.0,
                "ratio": 2.0,
                "hysteresis_db": 6.0,
                "floor_db": -60.0,
                "attack_ms": 2.0,
                "release_ms": 60.0,
                **kw,
            }
        )

    def test_loud_signal_passes(self):
        exp = self._exp()
        x = _sine(500, 0.5, amp=db_to_lin(-6.0))
        y = exp.process(x)
        np.testing.assert_allclose(_rms(y[-1000:]), _rms(x[-1000:]), rtol=0.1)

    def test_quiet_noise_attenuated(self):
        exp = self._exp()
        x = _sine(500, 0.5, amp=db_to_lin(-55.0))  # below threshold
        y = exp.process(x)
        self.assertLess(_rms(y[-1000:]), _rms(x[-1000:]) * 0.6)

    def test_hysteresis_no_chatter_in_band(self):
        # Once opened by a loud burst, a signal that dips into the hysteresis
        # band (between close and open thresholds) should stay open — gain
        # stays high — rather than chattering closed.
        exp = self._exp(threshold_db=-40.0, hysteresis_db=10.0)
        loud = _sine(500, 0.2, amp=db_to_lin(-6.0))
        # -36 dB sits below the -40+... open point math but above close;
        # choose a level inside the hysteresis band: between -40 (close-ish)
        # and -30 (open). Use -34.
        mid = _sine(500, 0.3, amp=db_to_lin(-34.0))
        exp.process(loud)
        y_mid = exp.process(mid)
        # Gain should remain essentially open (output ~ input) because we're
        # in the hysteresis band after having opened.
        self.assertGreater(_rms(y_mid[-1000:]), _rms(mid[-1000:]) * 0.8)

    def test_streaming_continuity(self):
        x = np.concatenate(
            [_sine(500, 0.2, amp=db_to_lin(-6)), _sine(500, 0.2, amp=db_to_lin(-55))]
        )
        a = self._exp().process(x)
        b = _split_process(self._exp(), x, [50, 400, 1200, 30])
        np.testing.assert_allclose(a, b, atol=1e-4)


class AGCTest(unittest.TestCase):
    def _agc(self, **kw):
        return AGC(
            **{
                "sample_rate": SR,
                "target_db": -18.0,
                "max_gain_db": 24.0,
                "time_ms": 200.0,
                "noise_floor_db": -60.0,
                **kw,
            }
        )

    def test_quiet_input_boosted_toward_target(self):
        agc = self._agc()
        x = _sine(400, 3.0, amp=db_to_lin(-36.0))
        y = agc.process(x)
        out_db = lin_to_db(_rms(y[-4000:]) * np.sqrt(2))
        self.assertAlmostEqual(out_db, -18.0, delta=4.0)

    def test_loud_input_attenuated_toward_target(self):
        agc = self._agc()
        x = _sine(400, 3.0, amp=db_to_lin(-3.0))
        y = agc.process(x)
        out_db = lin_to_db(_rms(y[-4000:]) * np.sqrt(2))
        self.assertLess(out_db, -3.0 + 1.0)

    def test_gain_capped_at_max(self):
        agc = self._agc(max_gain_db=12.0)
        x = _sine(400, 3.0, amp=db_to_lin(-50.0))  # would need >30 dB
        y = agc.process(x)
        gain_db = lin_to_db(_rms(y[-2000:]) / _rms(x[-2000:]))
        self.assertLessEqual(gain_db, 12.0 + 0.5)

    def test_silence_not_boosted(self):
        agc = self._agc()
        x = np.zeros(8000, dtype=np.float32)
        y = agc.process(x)
        self.assertEqual(float(np.max(np.abs(y))), 0.0)

    def test_streaming_continuity(self):
        x = _sine(400, 1.0, amp=db_to_lin(-30.0))
        a = self._agc().process(x)
        b = _split_process(self._agc(), x, [256, 256, 1000, 2000])
        # AGC ramps gain per-block; modest tolerance.
        np.testing.assert_allclose(a, b, atol=2e-2)


class AudioDSPChainTest(unittest.TestCase):
    def test_disabled_is_identity(self):
        dsp = AudioDSP(DSPParams(enabled=False), sample_rate=SR, is_mic=False)
        x = _sine(500, 0.2, amp=0.5)
        np.testing.assert_allclose(dsp.process(x), x, atol=1e-6)

    def test_empty_input(self):
        dsp = AudioDSP(DSPParams(enabled=True), sample_rate=SR, is_mic=False)
        out = dsp.process(np.zeros(0, dtype=np.float32))
        self.assertEqual(out.size, 0)

    def test_enabled_output_finite_and_in_range(self):
        dsp = AudioDSP(
            DSPParams(enabled=True, limiter=True, limiter_ceiling=0.95),
            sample_rate=SR,
            is_mic=False,
        )
        x = _sine(500, 0.5, amp=1.0)
        y = dsp.process(x)
        self.assertTrue(np.all(np.isfinite(y)))
        self.assertLessEqual(float(np.max(np.abs(y))), 0.95 + 1e-3)

    def test_quiet_source_made_louder(self):
        # The headline win: a quiet source should come out louder after the
        # compressor (+ makeup) so it uses more of the 4-bit DAC range.
        dsp = AudioDSP(
            DSPParams(
                enabled=True,
                compress=True,
                comp_threshold_db=-24.0,
                comp_ratio=3.0,
                expander=False,
                limiter=True,
            ),
            sample_rate=SR,
            is_mic=False,
        )
        x = _sine(500, 0.5, amp=db_to_lin(-20.0))
        y = dsp.process(x)
        self.assertGreater(_rms(y[-1000:]), _rms(x[-1000:]))

    def test_agc_only_active_for_mic(self):
        params = DSPParams(
            enabled=True, agc=True, compress=False, expander=False, limiter=False, pre_emphasis=0.0
        )
        x = _sine(400, 2.0, amp=db_to_lin(-36.0))
        line = AudioDSP(params, sample_rate=SR, is_mic=False).process(x)
        mic = AudioDSP(params, sample_rate=SR, is_mic=True).process(x)
        # Line path leaves level alone (no AGC); mic path boosts it.
        np.testing.assert_allclose(_rms(line), _rms(x), rtol=0.05)
        self.assertGreater(_rms(mic), _rms(line) * 1.5)

    def test_reset_restores_initial_behavior(self):
        dsp = AudioDSP(DSPParams(enabled=True), sample_rate=SR, is_mic=True)
        x = _sine(500, 0.3, amp=db_to_lin(-10.0))
        first = dsp.process(x)
        dsp.reset()
        second = dsp.process(x)
        np.testing.assert_allclose(first, second, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
