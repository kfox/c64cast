"""Unit tests for the Ultimate Audio FPGA PCM sampler (c64cast/sampler.py) and
its config/doctor integration. No hardware: a recording fake backend stands in
for the U64, and the doctor REST queries are mocked."""

from __future__ import annotations

import time
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np

from c64cast import config as cfgmod
from c64cast import doctor
from c64cast import sampler as s


# ---------------------------------------------------------------------------
# Pure register helpers
# ---------------------------------------------------------------------------
class PureHelperTest(unittest.TestCase):
    def test_divider_table_matches_doc(self):
        # round(6.25 MHz / rate); 44100 -> 142 is the documented value.
        self.assertEqual(s.divider_for_rate(44100), 142)
        self.assertEqual(s.divider_for_rate(48000), 130)
        self.assertEqual(s.divider_for_rate(8000), 781)
        self.assertEqual(s.divider_for_rate(16000), 391)

    def test_divider_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            s.divider_for_rate(0)

    def test_actual_rate_roundtrips(self):
        div = s.divider_for_rate(44100)
        self.assertAlmostEqual(s.actual_rate_for_divider(div), 6_250_000 / 142, places=2)

    def test_bytes_per_sample(self):
        self.assertEqual(s.bytes_per_sample(8), 1)
        self.assertEqual(s.bytes_per_sample(16), 2)
        with self.assertRaises(ValueError):
            s.bytes_per_sample(24)

    def test_pack_pcm_8bit_is_signed(self):
        arr = np.array([0, 32767, -32768, 256, -256], dtype=np.int16)
        out = np.frombuffer(s.pack_pcm(arr, 8), dtype=np.int8)
        self.assertEqual(list(out), [0, 127, -128, 1, -1])

    def test_pack_pcm_16bit_is_le(self):
        self.assertEqual(list(s.pack_pcm(np.array([1, -1], dtype=np.int16), 16)), [1, 0, 255, 255])

    def test_pack_pcm_rejects_bad_bits(self):
        with self.assertRaises(ValueError):
            s.pack_pcm(np.array([0], dtype=np.int16), 12)

    def test_control_byte_bits(self):
        self.assertEqual(s.control_byte(gate=True, repeat=True, bits=16), 0x13)
        self.assertEqual(s.control_byte(gate=True, bits=8), 0x01)
        self.assertEqual(s.control_byte(gate=False, repeat=True, bits=8), 0x02)
        self.assertEqual(s.control_byte(gate=True, interrupt=True, bits=16), 0x15)

    def test_channel_base(self):
        self.assertEqual(s.channel_base(0), 0xDF20)
        self.assertEqual(s.channel_base(1), 0xDF40)
        self.assertEqual(s.channel_base(6), 0xDFE0)
        with self.assertRaises(ValueError):
            s.channel_base(7)

    def test_channel_register_writes_layout(self):
        writes = dict(
            s.channel_register_writes(
                reu_offset=0x200000,
                length=0x100000,
                divider=142,
                volume=63,
                pan=7,
                repeat=True,
                repeat_a=0,
                repeat_b=0x100000,
            )
        )
        # Start address = $01000000 + REU offset, big-endian.
        self.assertEqual(writes[s.REG_START], [0x01, 0x20, 0x00, 0x00])
        self.assertEqual(writes[s.REG_LENGTH], [0x10, 0x00, 0x00])
        self.assertEqual(writes[s.REG_RATE], [0x00, 0x8E])  # 142
        self.assertEqual(writes[s.REG_VOLUME], [0x3F])
        self.assertEqual(writes[s.REG_PAN], [0x07])
        self.assertEqual(writes[s.REG_REPEAT_A], [0x00, 0x00, 0x00])
        self.assertEqual(writes[s.REG_REPEAT_B], [0x10, 0x00, 0x00])

    def test_register_writes_omit_repeat_when_off(self):
        writes = dict(
            s.channel_register_writes(
                reu_offset=0,
                length=100,
                divider=142,
                volume=63,
                pan=7,
                repeat=False,
                repeat_a=0,
                repeat_b=0,
            )
        )
        self.assertNotIn(s.REG_REPEAT_A, writes)
        self.assertNotIn(s.REG_REPEAT_B, writes)


# ---------------------------------------------------------------------------
# Recording fake backend for the streamer
# ---------------------------------------------------------------------------
class _FakeBackend:
    """Records the writes a UltimateAudioSampler issues (reu_write / write_regs /
    write_memory / flush). No socket, no REST."""

    def __init__(self) -> None:
        self.reu_writes: list[tuple[int, int]] = []  # (offset, length)
        self.reg_writes: list[tuple[str, tuple[int, ...]]] = []
        self.mem_writes: list[tuple[str, str]] = []
        self.flushes = 0

    def reu_write(self, offset: int, data: bytes) -> None:
        self.reu_writes.append((offset, len(data)))

    def write_regs(self, base_addr: str, *values: int) -> None:
        self.reg_writes.append((base_addr.upper(), values))

    def write_memory(self, address: str, data_hex: str) -> None:
        self.mem_writes.append((address.upper(), data_hex.upper()))

    def flush(self) -> None:
        self.flushes += 1


def _make(api: _FakeBackend, **kw) -> s.UltimateAudioSampler:
    """Build a sampler against the recording fake (cast like the audio tests'
    `cast(Ultimate64API, FakeAPI())` — the fake duck-types the write surface)."""
    return s.UltimateAudioSampler(cast(Any, api), **kw)


class StreamerTest(unittest.TestCase):
    def test_init_resolves_rate_and_ring(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16, ring_size=4097)
        self.assertEqual(smp.bps, 2)
        self.assertEqual(smp._divider, 142)
        self.assertEqual(smp.sample_rate, round(6_250_000 / 142))
        # Ring frame-aligned (even for 16-bit).
        self.assertEqual(smp.ring_size % 2, 0)
        self.assertTrue(smp.is_sampler)

    def test_write_wrapped_splits_at_ring_boundary(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=44100, bits=8, ring_base=0x200000, ring_size=16)
        smp._write_wrapped(10, b"ABCDEF")  # 6 bytes from pos 10 in a 16-byte ring
        # 6 bytes at base+10, then 0 wrap... 10+6=16 exactly, no wrap.
        self.assertEqual(api.reu_writes, [(0x200000 + 10, 6)])
        api.reu_writes.clear()
        smp._write_wrapped(12, b"ABCDEF")  # crosses: 4 at +12, 2 at +0
        self.assertEqual(api.reu_writes, [(0x200000 + 12, 4), (0x200000, 2)])

    def test_position_seconds_zero_before_start(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        self.assertEqual(smp.position_seconds(), 0.0)

    def test_position_seconds_tracks_wallclock(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 2.0
        self.assertAlmostEqual(smp.position_seconds(), 2.0, delta=0.2)

    def test_position_clamps_after_eof(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 100.0
        smp._pushed_samples = smp.sample_rate  # ~1 s of audio pushed
        smp.mark_eof()
        self.assertAlmostEqual(smp.position_seconds(), 1.0, delta=0.1)

    def test_read_consumed_bytes_is_frame_aligned(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 1.0
        consumed = smp._read_consumed_bytes()
        self.assertEqual(consumed % smp.bps, 0)
        self.assertGreater(consumed, 0)

    def test_start_prefills_and_gates_then_stop_gates_off(self):
        api = _FakeBackend()
        smp = _make(
            api, sample_rate=44100, bits=16, ring_base=0x200000, ring_size=8192, lead_seconds=0.01
        )
        # Prime the queue so the prebuffer returns immediately (no 2 s block).
        smp.push_samples(np.zeros(2048, dtype=np.int16))
        smp.start(prebuffer_timeout=0.1)
        try:
            # Prefill wrote the ring (NEUTRAL) before gating.
            self.assertTrue(api.reu_writes)
            # Control register at $DF20 was written with gate+repeat+mode16 (0x13).
            gate_writes = [v for a, v in api.mem_writes if a == "DF20"]
            self.assertIn("13", gate_writes)
            self.assertTrue(smp._running)
        finally:
            smp.stop()
        # Gate-off wrote $DF20 = 00.
        self.assertEqual(api.mem_writes[-1], ("DF20", "00"))
        self.assertFalse(smp._running)

    def test_prebuffer_target_decoupled_from_lead(self):
        # The runtime lead (1.0 s default) is deeper than the startup prebuffer
        # (0.5 s default), so playback starts promptly while the writer keeps a
        # cushion deep enough to ride out a 4K clip's decode stalls.
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        self.assertLess(smp._prebuffer_target, smp._lead_target)
        self.assertAlmostEqual(smp._prebuffer_target / smp._lead_target, 0.5, delta=0.05)

    def test_prebuffer_clamped_to_lead_target(self):
        # A prebuffer configured larger than the lead can't exceed the runtime
        # depth (the writer never targets less than it seeds).
        smp = _make(
            _FakeBackend(), sample_rate=44100, bits=16, lead_seconds=0.2, prebuffer_seconds=1.0
        )
        self.assertEqual(smp._prebuffer_target, smp._lead_target)

    def test_get_recent_samples_returns_pushed(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp.push_samples(np.ones(100, dtype=np.int16) * 16384)
        recent = smp.get_recent_samples(50)
        self.assertEqual(recent.shape, (50,))
        self.assertTrue(np.all(recent > 0.4))

    def test_set_pre_emphasis_is_noop(self):
        # Scene.setup calls this on the audio object regardless of backend.
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp.set_pre_emphasis(0.9)  # must not raise


# ---------------------------------------------------------------------------
# resolve_audio_backend + validate_sampler_cfg
# ---------------------------------------------------------------------------
class ResolveAudioBackendTest(unittest.TestCase):
    def test_auto_picks_sampler_when_available(self):
        self.assertEqual(
            cfgmod.resolve_audio_backend("auto", supports_sampler=True, sampler_available=True),
            "sampler",
        )

    def test_auto_falls_back_to_dac(self):
        self.assertEqual(
            cfgmod.resolve_audio_backend("auto", supports_sampler=True, sampler_available=False),
            "dac",
        )
        self.assertEqual(
            cfgmod.resolve_audio_backend("auto", supports_sampler=False, sampler_available=False),
            "dac",
        )

    def test_dac_is_forced(self):
        self.assertEqual(
            cfgmod.resolve_audio_backend("dac", supports_sampler=True, sampler_available=True),
            "dac",
        )

    def test_explicit_sampler_warns_and_falls_back(self):
        with self.assertLogs("c64cast.config", level="WARNING"):
            got = cfgmod.resolve_audio_backend(
                "sampler", supports_sampler=False, sampler_available=False
            )
        self.assertEqual(got, "dac")

    def test_explicit_sampler_succeeds_when_available(self):
        self.assertEqual(
            cfgmod.resolve_audio_backend("sampler", supports_sampler=True, sampler_available=True),
            "sampler",
        )


class ValidateSamplerCfgTest(unittest.TestCase):
    def _cfg(self, *, bits=16, rate=44100, enabled=True):
        cfg = cfgmod.Config()
        cfg.audio.enabled = enabled
        cfg.audio.sampler_bits = bits
        cfg.audio.sampler_sample_rate = rate
        return cfg

    def test_valid_passes(self):
        cfgmod.validate_sampler_cfg(self._cfg())  # no raise

    def test_bad_bits_rejected(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate_sampler_cfg(self._cfg(bits=12))

    def test_out_of_range_rate_rejected(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate_sampler_cfg(self._cfg(rate=96000))
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate_sampler_cfg(self._cfg(rate=10))

    def test_skipped_when_audio_disabled(self):
        # Even an invalid value is ignored when audio is off.
        cfgmod.validate_sampler_cfg(self._cfg(bits=99, enabled=False))


# ---------------------------------------------------------------------------
# doctor: availability + provisioning
# ---------------------------------------------------------------------------
class _FakeProfile:
    def __init__(self, supports_sampler: bool = True) -> None:
        self.supports_sampler = supports_sampler


class _FakeRestApi:
    """Category-aware fake: read_sampler_config GETs two config sections, so
    session.get must return the right one per URL."""

    def __init__(
        self,
        *,
        present: bool = True,
        map_status: str = "Enabled",
        vol_l: str = " 0 dB",
        vol_r: str = " 0 dB",
        supports_sampler: bool = True,
        put_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.base_url = "http://fake"
        self.profile = _FakeProfile(supports_sampler)
        self.put_calls: list[tuple[str, str, str]] = []
        self._put_error = put_error
        cart: dict[str, str] = {}
        mixer: dict[str, str] = {}
        if present:
            cart["Map Ultimate Audio $DF20-DFFF"] = map_status
            mixer["Vol Sampler L"] = vol_l
            mixer["Vol Sampler R"] = vol_r
        self._sections = {
            "C64 and Cartridge Settings": cart,
            "Audio Mixer": mixer,
        }
        self.session = mock.MagicMock()

        def _get(url, timeout=3.0):
            from urllib.parse import unquote

            if get_error is not None:
                raise get_error
            cat = unquote(url.split("/v1/configs/")[-1])
            resp = mock.MagicMock()
            resp.json.return_value = {cat: self._sections.get(cat, {}), "errors": []}
            resp.raise_for_status = mock.MagicMock()
            return resp

        self.session.get.side_effect = _get

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        if self._put_error is not None:
            raise self._put_error
        self.put_calls.append((category, item, value))


def _video_cfg(*, backend="auto", enabled=True, skip_probe=False):
    cfg = cfgmod.Config()
    cfg.audio.enabled = enabled
    cfg.audio.backend = backend
    cfg.debug.skip_probe = skip_probe
    cfg.scenes = [cfgmod.SceneCfg(type="video", file="x.mp4")]
    return cfg


class SamplerAvailabilityTest(unittest.TestCase):
    def test_available_when_mapped_and_audible(self):
        self.assertIs(doctor.sampler_is_available(_FakeRestApi()), True)

    def test_unavailable_when_map_disabled(self):
        self.assertIs(doctor.sampler_is_available(_FakeRestApi(map_status="Disabled")), False)

    def test_unavailable_when_muted(self):
        self.assertIs(doctor.sampler_is_available(_FakeRestApi(vol_l="OFF", vol_r="OFF")), False)

    def test_audible_when_one_channel_on(self):
        self.assertIs(doctor.sampler_is_available(_FakeRestApi(vol_r="OFF")), True)

    def test_unavailable_when_feature_absent(self):
        self.assertIs(doctor.sampler_is_available(_FakeRestApi(present=False)), False)

    def test_none_on_query_failure(self):
        import requests

        api = _FakeRestApi(get_error=requests.Timeout("read timeout"))
        self.assertIsNone(doctor.sampler_is_available(api))


class WantsSamplerTest(unittest.TestCase):
    def test_wants_with_auto_and_video(self):
        wants, reasons = doctor._wants_sampler(_video_cfg(backend="auto"))
        self.assertTrue(wants)
        self.assertTrue(reasons)

    def test_wants_with_explicit_sampler(self):
        self.assertTrue(doctor._wants_sampler(_video_cfg(backend="sampler"))[0])

    def test_not_wanted_with_dac(self):
        self.assertFalse(doctor._wants_sampler(_video_cfg(backend="dac"))[0])

    def test_not_wanted_without_audio(self):
        self.assertFalse(doctor._wants_sampler(_video_cfg(enabled=False))[0])

    def test_not_wanted_without_video_scene(self):
        cfg = cfgmod.Config()
        cfg.audio.enabled = True
        cfg.scenes = [cfgmod.SceneCfg(type="waveform", file="t.sid")]
        self.assertFalse(doctor._wants_sampler(cfg)[0])


class ProvisionSamplerTest(unittest.TestCase):
    def test_noop_when_already_enabled(self):
        api = _FakeRestApi(map_status="Enabled", vol_l=" 0 dB", vol_r=" 0 dB")
        self.assertIsNone(doctor.provision_sampler(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_enables_map_when_disabled(self):
        api = _FakeRestApi(map_status="Disabled")
        restore = doctor.provision_sampler(api, _video_cfg())
        self.assertIsNotNone(restore)
        self.assertIn(
            ("C64 and Cartridge Settings", "Map Ultimate Audio $DF20-DFFF", "Enabled"),
            api.put_calls,
        )
        # Restore maps the composite key back to "Disabled".
        assert restore is not None
        self.assertIn("Disabled", restore.values())

    def test_unmutes_when_off(self):
        api = _FakeRestApi(vol_l="OFF", vol_r="OFF")
        restore = doctor.provision_sampler(api, _video_cfg())
        assert restore is not None
        unmutes = [c for c in api.put_calls if c[0] == "Audio Mixer"]
        self.assertEqual(len(unmutes), 2)
        self.assertEqual(list(restore.values()).count("OFF"), 2)

    def test_skipped_on_no_sampler_backend(self):
        api = _FakeRestApi(supports_sampler=False, map_status="Disabled")
        self.assertIsNone(doctor.provision_sampler(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_skipped_under_skip_probe(self):
        api = _FakeRestApi(map_status="Disabled")
        self.assertIsNone(doctor.provision_sampler(api, _video_cfg(skip_probe=True)))
        self.assertEqual(api.put_calls, [])

    def test_skipped_when_backend_dac(self):
        api = _FakeRestApi(map_status="Disabled")
        self.assertIsNone(doctor.provision_sampler(api, _video_cfg(backend="dac")))
        self.assertEqual(api.put_calls, [])

    def test_restore_puts_originals_back(self):
        api = _FakeRestApi(map_status="Disabled", vol_l="OFF", vol_r=" 0 dB")
        restore = doctor.provision_sampler(api, _video_cfg())
        api.put_calls.clear()
        doctor.restore_sampler(api, restore)
        # Map restored to Disabled, the muted channel back to OFF.
        self.assertIn(
            ("C64 and Cartridge Settings", "Map Ultimate Audio $DF20-DFFF", "Disabled"),
            api.put_calls,
        )
        self.assertIn(("Audio Mixer", "Vol Sampler L", "OFF"), api.put_calls)

    def test_restore_noop_on_none(self):
        api = _FakeRestApi()
        doctor.restore_sampler(api, None)  # must not raise
        self.assertEqual(api.put_calls, [])


class WantsReuCouplingTest(unittest.TestCase):
    """The sampler streams its ring out of REU SDRAM, so a sampler run must
    pull the REU into _wants_reu (provisioning + the doctor REU probe)."""

    def test_sampler_makes_wants_reu_true(self):
        wants, reasons = doctor._wants_reu(_video_cfg(backend="auto"))
        self.assertTrue(wants)
        self.assertTrue(any("sampler" in r for r in reasons))

    def test_dac_video_does_not_want_reu(self):
        self.assertFalse(doctor._wants_reu(_video_cfg(backend="dac"))[0])


if __name__ == "__main__":
    unittest.main()
