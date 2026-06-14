"""Tests for the hardware abstraction layer (backend.py).

Phase 0 of the TeensyROM+ integration: the HAL must be a pure, behavior-
preserving seam over the existing Ultimate backend. These tests pin:
  * the capability ABC contract (mandatory vs gated methods),
  * the HardwareProfile / factory wiring,
  * that the choices vocabularies stay in sync with the backend registry.

They construct no hardware — `make_backend` is exercised via a fake that
stubs out the socket connect, and the ABC contract is checked structurally.
"""

# pyright: reportArgumentType=false
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from unittest import mock

from c64cast import config as cfgmod
from c64cast.backend import (
    BACKENDS,
    DELTA_CHUNK_BYTES,
    ULTIMATE_PROFILE,
    BackendCapabilityError,
    BufferedWriteBackend,
    C64Backend,
    make_backend,
)


class _RecordingBackend(BufferedWriteBackend):
    """Concrete BufferedWriteBackend whose transport just records emits.

    Lets the shared host-side write path (coalescing, delta cache, listeners,
    semantic helpers) be exercised with no hardware. `_emit` mirrors a real
    backend's contract: count the write, then route through the success ladder.
    Set `fail = True` to drive the failure ladder instead.
    """

    def __init__(self):
        super().__init__()
        self.emits: list[tuple[int, bytes]] = []
        self.fail = False

    def _emit(self, addr: int, payload: bytes) -> None:
        if self.fail:
            self._note_emit_failure(addr, RuntimeError("boom"))
            return
        self.emits.append((addr, bytes(payload)))
        self._stats["writes"] += 1
        self._note_emit_success()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def format_write_latency(self):
        return None


class ProfileAndRegistryTest(unittest.TestCase):
    def test_backend_choices_match_registry(self):
        # config._BACKEND_CHOICES is duplicated to keep config.py import-light;
        # it must mirror backend.BACKENDS exactly.
        self.assertEqual(tuple(cfgmod._BACKEND_CHOICES), tuple(BACKENDS))

    def test_ultimate_profile_is_fully_capable(self):
        p = ULTIMATE_PROFILE
        self.assertEqual(p.family, "ultimate")
        for flag in (
            "supports_write",
            "supports_read",
            "supports_reset",
            "supports_probe",
            "supports_run_prg",
            "supports_run_crt",
            "supports_reu",
            "reu_bus_clean",
            "kernal_irq_intact",
        ):
            self.assertTrue(getattr(p, flag), f"{flag} should be True")
        # The U64 DMAWRITE is fire-and-forget, not acked.
        self.assertFalse(p.writes_are_acked)
        self.assertEqual(p.write_transport, "socket_dma")
        # No per-variant fps cap on the Ultimate.
        self.assertIsNone(p.max_fps)

    def test_profile_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            ULTIMATE_PROFILE.default_fps = 30.0  # type: ignore[misc]


class AbstractContractTest(unittest.TestCase):
    def test_cannot_instantiate_bare_abc(self):
        with self.assertRaises(TypeError):
            C64Backend()  # type: ignore[abstract]

    def test_minimal_backend_gates_unsupported_capabilities(self):
        # A backend that implements only the mandatory write surface should
        # construct fine, and every capability-gated method raises until
        # overridden.
        class MinimalBackend(C64Backend):
            profile = replace(
                ULTIMATE_PROFILE,
                name="min",
                family="tr",
                supports_read=False,
                supports_reset=False,
                supports_run_prg=False,
                supports_reu=False,
            )

            def write_memory(self, address, data_hex): ...
            def write_memory_file(self, address, data_bytes): ...
            def write_regs(self, base_addr, *values): ...
            def write_region(self, address, data, region_id=None, full_threshold=0.6):
                return 0

            def flush(self): ...
            def close(self): ...
            def invalidate_cache(self): ...
            def add_write_listener(self, callback): ...
            def remove_write_listener(self, callback): ...
            def format_write_latency(self):
                return None

            @property
            def stats(self):
                return {}

        b = MinimalBackend()
        # probe is a soft default (returns None, doesn't raise).
        self.assertIsNone(b.probe())
        for call in (
            lambda: b.read_memory(0x028D, 1),
            lambda: b.reset(),
            lambda: b.run_basic_clear_loop(),
            lambda: b.launch_program("x.prg"),
            lambda: b.run_sid_player(b"PSID"),
            lambda: b.cue_song_reinit(1),
            lambda: b.reu_write(0, b"\x00"),
            lambda: b.silence_sid(),
            lambda: b.restore_kernal_irq_vector(),
            lambda: b.suppress_cursor_blink(),
            lambda: b.disable_case_switch(),
        ):
            with self.assertRaises(BackendCapabilityError):
                call()


class MakeBackendTest(unittest.TestCase):
    def _cfg(self, *, backend="ultimate", system="NTSC"):
        cfg = cfgmod.Config()
        cfg.hardware.backend = backend
        cfg.ultimate64.system = system
        return cfg

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            make_backend(self._cfg(backend="nope"))

    def test_ultimate_backend_constructed_with_profile(self):
        # Patch the socket connect so no hardware is needed; assert the
        # factory builds an Ultimate64API carrying a profile whose
        # default_fps was resolved from the configured video system.
        with mock.patch("c64cast.socket_dma.SocketDMAClient.connect"):
            from c64cast.api import Ultimate64API

            api = make_backend(self._cfg(system="NTSC"))
            self.assertIsInstance(api, Ultimate64API)
            self.assertIsInstance(api, C64Backend)
            self.assertEqual(api.profile.default_fps, 60.0)
            self.assertEqual(api.profile.family, "ultimate")

            api_pal = make_backend(self._cfg(system="PAL"))
            self.assertEqual(api_pal.profile.default_fps, 50.0)

    def test_direct_construction_defaults_to_ultimate_profile(self):
        with mock.patch("c64cast.socket_dma.SocketDMAClient.connect"):
            from c64cast.api import Ultimate64API

            api = Ultimate64API("http://example.lan")
            self.assertIs(api.profile, ULTIMATE_PROFILE)

    def test_ultimate_supports_reu_via_backend_surface(self):
        # reu_write is the backend-agnostic REU entry point; on the Ultimate
        # it forwards to the socket client's reuwrite.
        with mock.patch("c64cast.socket_dma.SocketDMAClient.connect"):
            from c64cast.api import Ultimate64API

            api = Ultimate64API("http://example.lan")
            with mock.patch.object(api.socket_dma, "reuwrite") as rw:
                api.reu_write(0x100000, b"\x01\x02")
                rw.assert_called_once_with(0x100000, b"\x01\x02")


class BufferedWriteBackendTest(unittest.TestCase):
    """The host-side write path shared by every concrete backend."""

    def _b(self):
        return _RecordingBackend()

    # ---- basic writes + coalescing ---------------------------------------
    def test_write_memory_hex(self):
        b = self._b()
        b.write_memory("d020", "0e")
        self.assertEqual(b.emits, [(0xD020, b"\x0e")])

    def test_write_memory_file_counts_bytes(self):
        b = self._b()
        b.write_memory_file("0400", b"\x01\x02\x03")
        self.assertEqual(b.emits, [(0x0400, b"\x01\x02\x03")])
        self.assertEqual(b.stats["bytes"], 3)

    def test_write_regs_coalesces_into_one_emit(self):
        b = self._b()
        b.write_regs("d020", 0x0E, 0x06, 0x01, 0x02)
        # Four register values become a single contiguous transport write.
        self.assertEqual(b.emits, [(0xD020, b"\x0e\x06\x01\x02")])

    def test_write_regs_masks_to_byte(self):
        b = self._b()
        b.write_regs("d020", 0x1FF)  # overflow masked to 0xFF
        self.assertEqual(b.emits, [(0xD020, b"\xff")])

    # ---- write_region delta cache ----------------------------------------
    def test_region_full_upload_then_skip_unchanged(self):
        b = self._b()
        data = bytes([0]) * 100
        self.assertEqual(b.write_region(0x0400, data, region_id=1), 100)
        # Identical second push → nothing emitted, skip counted.
        self.assertEqual(b.write_region(0x0400, data, region_id=1), 0)
        self.assertEqual(len(b.emits), 1)
        self.assertEqual(b.stats["skipped"], 1)

    def test_region_narrow_span_uploads_only_diff(self):
        b = self._b()
        base = bytearray(100)
        b.write_region(0x0400, bytes(base), region_id=1)
        b.emits.clear()
        base[10] = 0xAA
        base[11] = 0xBB
        span = b.write_region(0x0400, bytes(base), region_id=1)
        # Only the changed sub-range is pushed, at the offset of the first diff.
        self.assertEqual(span, 2)
        self.assertEqual(b.emits, [(0x0400 + 10, b"\xaa\xbb")])

    def test_region_length_change_forces_full_upload(self):
        b = self._b()
        b.write_region(0x0400, bytes(50), region_id=1)
        b.emits.clear()
        n = b.write_region(0x0400, bytes(80), region_id=1)
        self.assertEqual(n, 80)
        self.assertEqual(b.emits[0][0], 0x0400)

    def test_region_chunked_diff_for_sparse_wide_range(self):
        # A buffer with two distant single-byte changes spans nearly the whole
        # region (wide dirty range) but only a couple of chunks differ → the
        # chunked-diff path uploads just those chunks, not the whole buffer.
        b = self._b()
        n = DELTA_CHUNK_BYTES * 8
        base = bytearray(n)
        b.write_region(0x4000, bytes(base), region_id=2)
        b.emits.clear()
        base[5] = 1  # chunk 0
        base[n - 5] = 1  # last chunk
        uploaded = b.write_region(0x4000, bytes(base), region_id=2)
        # Two dirty chunks of DELTA_CHUNK_BYTES each — far less than n.
        self.assertEqual(uploaded, DELTA_CHUNK_BYTES * 2)
        self.assertEqual(len(b.emits), 2)
        self.assertLess(uploaded, n)

    def test_region_full_fallback_when_dirty_everywhere(self):
        # Every chunk dirty → chunked diff saves nothing → one full push.
        b = self._b()
        n = DELTA_CHUNK_BYTES * 4
        b.write_region(0x4000, bytes(n), region_id=3)
        b.emits.clear()
        changed = bytes([0xFF]) * n
        uploaded = b.write_region(0x4000, changed, region_id=3)
        self.assertEqual(uploaded, n)
        self.assertEqual(len(b.emits), 1)
        self.assertEqual(b.emits[0], (0x4000, changed))

    def test_invalidate_cache_forces_next_full_upload(self):
        b = self._b()
        data = bytes(40)
        b.write_region(0x0400, data, region_id=1)
        b.invalidate_cache()
        b.emits.clear()
        # Cache dropped → identical data re-uploads in full instead of skipping.
        self.assertEqual(b.write_region(0x0400, data, region_id=1), 40)
        self.assertEqual(len(b.emits), 1)

    def test_region_accepts_bytearray(self):
        b = self._b()
        self.assertEqual(b.write_region(0x0400, bytearray(b"\x01\x02"), region_id=1), 2)

    # ---- listeners -------------------------------------------------------
    def test_listeners_receive_writes_and_can_be_removed(self):
        b = self._b()
        seen: list[tuple[int, bytes]] = []
        cb = lambda a, d: seen.append((a, bytes(d)))  # noqa: E731
        b.add_write_listener(cb)
        b.write_memory("d020", "0e")
        b.write_memory_file("0400", b"\x01")
        self.assertEqual(seen, [(0xD020, b"\x0e"), (0x0400, b"\x01")])
        b.remove_write_listener(cb)
        b.write_memory("d021", "06")
        self.assertEqual(len(seen), 2)  # no new notifications
        # Removing an unregistered callback is a no-op (suppressed ValueError).
        b.remove_write_listener(cb)

    def test_listener_exception_does_not_break_write(self):
        b = self._b()
        b.add_write_listener(lambda a, d: (_ for _ in ()).throw(RuntimeError()))
        with self.assertLogs("c64cast.backend", level="ERROR"):
            b.write_memory("d020", "0e")  # must not propagate
        self.assertEqual(b.emits, [(0xD020, b"\x0e")])

    # ---- failure ladder --------------------------------------------------
    def test_emit_failure_ladder_logs_and_counts(self):
        b = self._b()
        b.fail = True
        with self.assertLogs("c64cast.backend", level="WARNING") as cap:
            for _ in range(50):
                b.write_memory("d020", "00")
        self.assertEqual(b.stats["errors"], 50)
        self.assertTrue(any("consecutive" in line for line in cap.output))

    def test_emit_success_resets_consecutive_counter(self):
        b = self._b()
        b.fail = True
        b.write_memory("d020", "00")  # one failure
        b.fail = False
        b.write_memory("d020", "00")  # success clears the counter
        self.assertEqual(b._consecutive_errors, 0)

    # ---- semantic write helpers ------------------------------------------
    def test_silence_sid_clears_volume_and_gates(self):
        b = self._b()
        b.silence_sid()
        addrs = [a for a, _ in b.emits]
        self.assertIn(0xD418, addrs)  # master volume
        # One gate-clear per voice.
        self.assertEqual(b.emits[0], (0xD418, b"\x00"))
        self.assertGreaterEqual(len(b.emits), 4)

    def test_blank_display_clears_den(self):
        b = self._b()
        b.blank_display()
        self.assertEqual(b.emits, [(0xD011, b"\x0b")])  # DEN bit cleared

    def test_disable_case_switch(self):
        b = self._b()
        b.disable_case_switch()
        self.assertEqual(b.emits, [(0x0291, b"\x80")])

    def test_restore_kernal_irq_vector(self):
        b = self._b()
        b.restore_kernal_irq_vector()
        # $0314/$0315 ← $EA31 (kernal default), coalesced into one write.
        self.assertEqual(b.emits, [(0x0314, b"\x31\xea")])

    def test_suppress_cursor_blink(self):
        b = self._b()
        b.suppress_cursor_blink()
        self.assertEqual(b.emits, [(0x00CC, b"\x80")])


class MakeBackendTeensyromValidationTest(unittest.TestCase):
    """The cheap error paths in make_backend's teensyrom branch — no real
    serial/TCP transport is constructed because validation fails first."""

    def _cfg(self, **tr):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        for k, v in tr.items():
            setattr(cfg.teensyrom, k, v)
        return cfg

    def test_serial_requires_port(self):
        cfg = self._cfg(transport="serial", serial_port="")
        with self.assertRaises(ValueError) as ctx:
            make_backend(cfg)
        self.assertIn("serial_port", str(ctx.exception))

    def test_tcp_requires_host(self):
        cfg = self._cfg(transport="tcp", host="")
        with self.assertRaises(ValueError) as ctx:
            make_backend(cfg)
        self.assertIn("host", str(ctx.exception))

    def test_unknown_transport_raises(self):
        cfg = self._cfg(transport="carrier-pigeon")
        with self.assertRaises(ValueError) as ctx:
            make_backend(cfg)
        self.assertIn("transport", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
