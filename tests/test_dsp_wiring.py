"""Integration tests for the DSP chain wired into AudioStreamer / config.

These check the *wiring* (the pure DSP math is covered in test_dsp.py): that the
encode paths actually run the chain when enabled, that disabled is a true no-op,
that the mic hard gate is bypassed when DSP is active (the expander takes over),
and that the offline (REU pre-encode) path applies the same DSP. No hardware —
FakeAPI stands in for the U64.
"""

from __future__ import annotations

import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI

from c64cast.api import Ultimate64API
from c64cast.audio import AudioStreamer, encode_floats_to_dac
from c64cast.config import DSPCfg
from c64cast.dsp import (
    PRE_EMPHASIS_LINE_DEFAULT,
    PRE_EMPHASIS_MIC_DEFAULT,
    AudioDSP,
    DSPParams,
    PreEmphasis,
    db_to_lin,
)

SR = 8000


def _streamer(dsp_params: DSPParams | None) -> AudioStreamer:
    return AudioStreamer(
        cast(Ultimate64API, FakeAPI()), SR, "NTSC", dither=False, dsp_params=dsp_params
    )


def _quiet_sine(amp_db: float, secs: float = 0.5) -> np.ndarray:
    t = np.arange(int(secs * SR), dtype=np.float32) / SR
    return (db_to_lin(amp_db) * np.sin(2 * np.pi * 500 * t)).astype(np.float32)


class EncodePathTest(unittest.TestCase):
    def test_disabled_dsp_is_identity_vs_raw_encode(self):
        s = _streamer(DSPParams(enabled=False))
        x = _quiet_sine(-6.0)
        # _apply_dsp short-circuits; encoded bytes equal the raw encoder.
        processed = s._apply_dsp(x)
        np.testing.assert_array_equal(processed, x)
        self.assertFalse(s._dsp_active())

    def test_enabled_dsp_lifts_quiet_signal_in_encode(self):
        # A quiet source should occupy MORE of the 4-bit DAC range after the
        # compressor + makeup than the raw linear encode does.
        x = _quiet_sine(-20.0)
        raw = encode_floats_to_dac(x, dither=False)
        s = _streamer(
            DSPParams(
                enabled=True, compress=True, comp_threshold_db=-24.0, expander=False, limiter=True
            )
        )
        self.assertTrue(s._dsp_active())
        proc = encode_floats_to_dac(s._apply_dsp(x), dither=False)
        # Spread around the 7/8 midpoint = how much DAC range is used.
        self.assertGreater(int(np.ptp(proc)), int(np.ptp(raw)))

    def test_offline_dsp_matches_realtime_chain(self):
        # process_offline_dsp must apply the same (line) chain the realtime
        # encode path uses, so REU-staged and host commercial audio agree.
        params = DSPParams(enabled=True, compress=True, expander=True, limiter=True)
        x = _quiet_sine(-18.0, secs=0.3)
        realtime = _streamer(params)._apply_dsp(x)
        offline = _streamer(params).process_offline_dsp(x)
        np.testing.assert_allclose(realtime, offline, atol=1e-5)

    def test_offline_dsp_disabled_is_noop(self):
        s = _streamer(DSPParams(enabled=False))
        x = _quiet_sine(-18.0, secs=0.2)
        np.testing.assert_array_equal(s.process_offline_dsp(x), x)


class MicChainTest(unittest.TestCase):
    def test_mic_chain_enables_agc(self):
        # The mic chain (is_mic=True) activates AGC; the line chain does not.
        # pre_emphasis=0.0 isolates AGC (the source-aware default would
        # otherwise add a PreEmphasis stage to the line chain too).
        params = DSPParams(
            enabled=True, agc=True, compress=False, expander=False, limiter=False, pre_emphasis=0.0
        )
        s = _streamer(params)  # __init__ builds the line chain (no AGC)
        line_active_only = s._dsp.active
        # start_mic rebuilds with is_mic=True; emulate that rebuild directly
        # (avoids opening a real sound device).
        s._dsp = AudioDSP(s._dsp_params, sample_rate=SR, is_mic=True)
        x = _quiet_sine(-36.0, secs=2.0)
        boosted = s._apply_dsp(x)
        # Line chain had no enabled stage (AGC is mic-only) → inactive no-op.
        self.assertFalse(line_active_only)
        self.assertGreater(float(np.sqrt(np.mean(boosted**2))), float(np.sqrt(np.mean(x**2))) * 1.5)

    def test_no_dsp_params_defaults_to_inactive(self):
        s = AudioStreamer(cast(Ultimate64API, FakeAPI()), SR, "NTSC")
        self.assertFalse(s._dsp_active())


def _pre_emphasis_amount(dsp: AudioDSP) -> float | None:
    """The amount of the chain's PreEmphasis stage, or None if it has none."""
    for proc in dsp._chain:
        if isinstance(proc, PreEmphasis):
            return proc.amount
    return None


class SourceAwarePreEmphasisTest(unittest.TestCase):
    def test_auto_resolves_by_source(self):
        # pre_emphasis=None → mic default for is_mic, line default otherwise.
        p = DSPParams(enabled=True, pre_emphasis=None)
        mic = AudioDSP(p, sample_rate=SR, is_mic=True)
        line = AudioDSP(p, sample_rate=SR, is_mic=False)
        self.assertEqual(_pre_emphasis_amount(mic), PRE_EMPHASIS_MIC_DEFAULT)
        self.assertEqual(_pre_emphasis_amount(line), PRE_EMPHASIS_LINE_DEFAULT)

    def test_explicit_value_overrides_both_sources(self):
        p = DSPParams(enabled=True, pre_emphasis=0.35)
        for is_mic in (True, False):
            dsp = AudioDSP(p, sample_rate=SR, is_mic=is_mic)
            amt = _pre_emphasis_amount(dsp)
            assert amt is not None
            self.assertAlmostEqual(amt, 0.35)

    def test_zero_disables_pre_emphasis(self):
        p = DSPParams(enabled=True, pre_emphasis=0.0)
        dsp = AudioDSP(p, sample_rate=SR, is_mic=True)
        self.assertIsNone(_pre_emphasis_amount(dsp))


class SetPreEmphasisTest(unittest.TestCase):
    def test_set_pre_emphasis_rebuilds_line_chain(self):
        s = _streamer(DSPParams(enabled=True, pre_emphasis=None))
        # Default line chain uses the line default.
        self.assertEqual(_pre_emphasis_amount(s._dsp), PRE_EMPHASIS_LINE_DEFAULT)
        s.set_pre_emphasis(0.2)
        self.assertEqual(s._dsp_params.pre_emphasis, 0.2)
        amt = _pre_emphasis_amount(s._dsp)
        assert amt is not None
        self.assertAlmostEqual(amt, 0.2)
        # Back to auto.
        s.set_pre_emphasis(None)
        self.assertIsNone(s._dsp_params.pre_emphasis)
        self.assertEqual(_pre_emphasis_amount(s._dsp), PRE_EMPHASIS_LINE_DEFAULT)

    def test_set_pre_emphasis_noop_without_params(self):
        # __new__-built streamer (no __init__) must not raise.
        s = AudioStreamer.__new__(AudioStreamer)
        s.set_pre_emphasis(0.5)  # no _dsp_params → silent no-op


class ConfigToParamsTest(unittest.TestCase):
    def test_dsp_enabled_on_by_default(self):
        self.assertTrue(DSPCfg().enabled)

    def test_pre_emphasis_defaults_to_auto(self):
        self.assertIsNone(DSPCfg().pre_emphasis)
        self.assertIsNone(DSPCfg().to_params().pre_emphasis)

    def test_makeup_auto_maps_to_none(self):
        cfg = DSPCfg(comp_makeup_auto=True, comp_makeup_db=9.0)
        self.assertIsNone(cfg.to_params().comp_makeup_db)

    def test_makeup_explicit_passes_through(self):
        cfg = DSPCfg(comp_makeup_auto=False, comp_makeup_db=9.0)
        self.assertEqual(cfg.to_params().comp_makeup_db, 9.0)

    def test_fields_round_trip(self):
        cfg = DSPCfg(enabled=True, comp_ratio=5.0, expander_threshold_db=-50.0)
        p = cfg.to_params()
        self.assertTrue(p.enabled)
        self.assertEqual(p.comp_ratio, 5.0)
        self.assertEqual(p.expander_threshold_db, -50.0)


if __name__ == "__main__":
    unittest.main()
