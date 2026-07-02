"""Tests for the Mahoney 8-bit $D418 companding curves: the resolver, the
pure encoder branch, and the SID env / ring bring-up the AudioStreamer does
when a curve is active. No real hardware."""

from __future__ import annotations

import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI

from c64cast.api import Ultimate64API
from c64cast.audio import (
    NEUTRAL_SAMPLE,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
    SID_GATE_OFF,
    AudioStreamer,
    encode_floats_to_dac,
)
from c64cast.dac_curves import (
    DAC_CURVE_CHOICES,
    MAHONEY_ULTISID,
    NEUTRAL_INDEX,
    resolve_dac_curve,
)


class ResolveDacCurveTest(unittest.TestCase):
    def test_linear_resolves_to_none(self):
        self.assertIsNone(resolve_dac_curve("linear"))

    def test_mahoney_ultisid_is_256_bytes(self):
        table = resolve_dac_curve("mahoney_ultisid")
        assert table is not None
        self.assertEqual(len(table), 256)
        self.assertTrue(all(0 <= b <= 255 for b in table))

    def test_unknown_curve_raises(self):
        with self.assertRaises(ValueError):
            resolve_dac_curve("nope")

    def test_choices_list(self):
        # "auto" + "calibrated" are system-aware (resolved in dac_calibration);
        # the baked names resolve here.
        self.assertEqual(DAC_CURVE_CHOICES, ["auto", "linear", "mahoney_ultisid", "calibrated"])

    def test_system_aware_names_are_not_baked_tables(self):
        for name in ("auto", "calibrated"):
            with self.assertRaises(ValueError):
                resolve_dac_curve(name)


class EncodeCurveTest(unittest.TestCase):
    def test_linear_path_bit_identical(self):
        # curve=None must reproduce the historical (x+1)*7.5 → clip → uint8.
        rng = np.random.default_rng(1)
        x = (rng.random(2048).astype(np.float32) * 2.0) - 1.0
        x[::5] = 0.0
        expected = np.clip((x + 1.0) * 7.5, 0, 15).astype(np.uint8)
        got = encode_floats_to_dac(x, dither=False)
        np.testing.assert_array_equal(got, expected)

    def test_curve_zero_maps_to_neutral_index_byte(self):
        curve = np.frombuffer(MAHONEY_ULTISID, dtype=np.uint8)
        out = encode_floats_to_dac(np.zeros(16, np.float32), dither=True, curve=curve)
        self.assertTrue(np.all(out == curve[NEUTRAL_INDEX]))

    def test_curve_endpoints(self):
        curve = np.frombuffer(MAHONEY_ULTISID, dtype=np.uint8)
        hi = encode_floats_to_dac(np.array([1.0], np.float32), dither=False, curve=curve)
        lo = encode_floats_to_dac(np.array([-1.0], np.float32), dither=False, curve=curve)
        self.assertEqual(int(hi[0]), int(curve[255]))
        self.assertEqual(int(lo[0]), int(curve[0]))

    def test_curve_output_is_bounds_safe(self):
        # Extreme (out-of-range) inputs must still index the table safely.
        curve = np.frombuffer(MAHONEY_ULTISID, dtype=np.uint8)
        out = encode_floats_to_dac(np.array([5.0, -5.0], np.float32), dither=False, curve=curve)
        self.assertEqual(int(out[0]), int(curve[255]))
        self.assertEqual(int(out[1]), int(curve[0]))


def _bare_streamer(api: FakeAPI, dac_curve: str) -> AudioStreamer:
    """AudioStreamer with just the attrs the env / bring-up paths touch."""
    s = AudioStreamer.__new__(AudioStreamer)
    s.api = cast(Ultimate64API, api)
    s.digi_boost = False
    s.dac_curve_name = dac_curve
    table = resolve_dac_curve(dac_curve)
    if table is not None:
        s._dac_curve = np.frombuffer(table, dtype=np.uint8)
        s._neutral_byte = int(s._dac_curve[NEUTRAL_INDEX])
    else:
        s._dac_curve = None
        s._neutral_byte = NEUTRAL_SAMPLE
    return s


class MahoneyEnvTest(unittest.TestCase):
    def test_enable_writes_env_block(self):
        api = FakeAPI()
        s = _bare_streamer(api, "mahoney_ultisid")
        s._enable_mahoney_env()
        # 3 voices: control ($49) + AD/SR pair ($0F, $FF).
        for base in (0xD400, 0xD407, 0xD40E):
            self.assertEqual(api.memories[f"{base + 4:04X}"], "49")
            self.assertEqual(api.regs[f"{base + 5:04X}"], (0x0F, 0xFF))
        # Filter cutoff maxed ($D415/$D416) + route voices 1+2, res 0 ($D417).
        self.assertEqual(api.regs["D415"], (0xFF, 0xFF))
        self.assertEqual(api.memories["D417"], "03")

    def test_disable_gates_all_voices_off(self):
        api = FakeAPI()
        s = _bare_streamer(api, "mahoney_ultisid")
        s._disable_mahoney_env()
        for base in (0xD400, 0xD407, 0xD40E):
            self.assertEqual(api.memories[f"{base + 4:04X}"], f"{SID_GATE_OFF:02X}")

    def test_upload_engages_env_and_neutral_ring_when_curved(self):
        api = FakeAPI()
        s = _bare_streamer(api, "mahoney_ultisid")
        s._upload_nmi_and_buffers()
        # Ring prefilled with the curve's silence byte, not 4-bit NEUTRAL_SAMPLE.
        ring = api.mem_files[f"{RING_BUFFER_ADDR:04X}"]
        self.assertEqual(len(ring), RING_BUFFER_SIZE)
        self.assertEqual(set(ring), {int(MAHONEY_ULTISID[NEUTRAL_INDEX])})
        # Mahoney env was installed (filter routing write present).
        self.assertEqual(api.memories["D417"], "03")

    def test_upload_linear_leaves_env_untouched(self):
        api = FakeAPI()
        s = _bare_streamer(api, "linear")
        s._upload_nmi_and_buffers()
        ring = api.mem_files[f"{RING_BUFFER_ADDR:04X}"]
        self.assertEqual(set(ring), {NEUTRAL_SAMPLE})
        self.assertNotIn("D417", api.memories)


if __name__ == "__main__":
    unittest.main()
