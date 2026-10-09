"""Tests for the REU-staged audio path (AudioStreamer.start_for_reu_staged).

These tests don't require a real U64 — they verify the bring-up sequence
(REU upload, NMI install, IRQ vector patch order) against the FakeAPI's
recorded write log."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import cast
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
from _fakes import (
    IRQ_ENTRY_A,
    RTI_RETURN_ADDR,
    FakeAPI,
    lose_reu_writes_to,
    lose_writes_to,
    new_streamer,
    quiet_logging,
    run_irq_handler,
    written_addresses,
)

from c64cast.audio import audio as audio_mod
from c64cast.audio.audio import AudioStreamer, PumpInstallError
from c64cast.audio.audio_handlers import (
    NEUTRAL_SAMPLE,
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    READ_PTR_HI_ADDR,
    READ_PTR_LO_ADDR,
    REU_AUDIO_BASE,
    REU_AUDIO_MAX_BYTES,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_GOVERNOR_GAP_THRESHOLD_HI,
    REU_GOVERNOR_OVERTAKE_GAP_HI,
    REU_GOVERNOR_PUMP_OVERDRIVE,
    REU_IRQ_HANDLER,
    REU_IRQ_HANDLER_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH_8KHZ,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_HANDLER_STUB,
    REU_PUMP_INITIAL_MARGIN,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    RING_BUFFER_SIZE,
    RING_LEAD_EMA_ALPHA,
    patch_chunk_size,
)
from c64cast.audio.audio_servo import (
    HOST_DMA_SERVO_INTEG_CLAMP,
    HOST_DMA_SERVO_PERIOD_MAX_FRAC,
    HOST_DMA_SERVO_PERIOD_MIN_FRAC,
    HOST_DMA_SERVO_TARGET_GAP,
    servo_hold_period,
    servo_period,
)
from c64cast.hw.c64 import CIA1, CIA_TIMER_LATCH_MAX, KERNAL, REU, VECTORS, kernal_cia1_latch
from c64cast.hw.socket_dma import SocketDMAError
from c64cast.scenes.scenes import VideoScene

# The matched pump latch at the fixture's rate, spelled out rather than
# derived: at 12 kHz NTSC (the shipped [audio].sample_rate default) the NMI
# latch is round(1022727/12000) - 1 = 84, so the NMI period is 85 cycles and a
# 128-byte chunk needs 128 x 85 - 1 = 10879. Independent arithmetic, so a
# change to the production derivation has to face a number rather than itself.
MATCHED_LATCH_12KHZ = 10879


def _packed_latch(latch: int) -> str:
    """The CIA #1 Timer A latch as write_memory records it (LO then HI)."""
    return f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"


def _new_streamer(use_reu_pump: bool = True, **overrides) -> AudioStreamer:
    """This file's defaults over the shared builder: dither OFF, and
    reu_pump_governor OFF so the bring-up tests below assert the plain
    open-loop handler bytes (GovernorSelectionTest flips it on explicitly;
    the production default is True — see config.AudioCfg.reu_pump_governor)."""
    return new_streamer(
        dither=False, use_reu_pump=use_reu_pump, reu_pump_governor=False, **overrides
    )


class RingBufferRelocationTest(unittest.TestCase):
    """The audio ring lives at $4000 (not $8000) so it stays out of VIC
    bank 2, which the REU-staged display modes use as the off-screen swap
    target. The REU IRQ handler embeds the ring bounds as immediates;
    NmiRoutineTest runs the NMI routine across the same bounds."""

    def test_ring_is_in_vic_bank_1(self):
        # VIC banks: 0=$0000-$3FFF, 1=$4000-$7FFF, 2=$8000-$BFFF, 3=$C000-$FFFF.
        # Bank 1 is the only one c64cast never selects in PETSCII / blank
        # mode (banks 0 + 2 have kernal char-ROM mapped at $1000/$9000).
        # If the ring address drifts into bank 0 or 2, REU-staged display
        # mode would race against VIC and draw audio samples as garbage.
        self.assertGreaterEqual(
            RING_BUFFER_ADDR, 0x4000, "ring must not overlap VIC bank 0 ($0000-$3FFF)"
        )
        self.assertLess(
            RING_BUFFER_ADDR + RING_BUFFER_SIZE,
            0x8000,
            "ring must not extend into VIC bank 2 ($8000+)",
        )

    def test_reu_handler_wrap_check_uses_relocated_end(self):
        # The REU IRQ handler embeds RING_BUFFER_END_HI directly in its
        # CMP #end_hi byte at offset 20 (see REU_IRQ_HANDLER comment).
        # If the ring relocates and the handler bytes aren't regenerated,
        # the pump would never wrap and silently overrun into whatever
        # lives past the ring.
        self.assertEqual(REU_IRQ_HANDLER[19], 0xC9, "CMP immediate opcode")
        self.assertEqual(REU_IRQ_HANDLER[20], RING_BUFFER_END_HI)
        self.assertEqual(REU_IRQ_HANDLER[23], 0xA9, "LDA immediate opcode")
        self.assertEqual(REU_IRQ_HANDLER[24], RING_BUFFER_HI)


class NmiRoutineTest(unittest.TestCase):
    """NMI_ROUTINE, uploaded by the bring-up and EXECUTED on py65 from an
    interrupt frame until its RTI. It runs once per sample (~12000 times a
    second), so a branch that misses its PLA leaves the pushed A on the
    stack and the RTI returns to a garbage address: the C64 crashes on the
    first sample that does not cross a page."""

    def _run(self, r: int, sample: int = 0x0B):
        seed = {READ_PTR_LO_ADDR: r & 0xFF, READ_PTR_HI_ADDR: r >> 8, r: sample}
        return run_irq_handler(NMI_ROUTINE, addr=NMI_ROUTINE_ADDR, seed=seed, rti=True)

    def _r(self, run) -> int:
        ram = run.memory.ram
        return ram[READ_PTR_LO_ADDR] | (ram[READ_PTR_HI_ADDR] << 8)

    def _assert_clean_return(self, run) -> None:
        from c64cast.hw.c64 import CIA2

        self.assertEqual(
            run.exit_pc, RTI_RETURN_ADDR, "the RTI must return to the interrupted code"
        )
        self.assertEqual(run.mpu.sp, 0xFF, "PHA/PLA and the RTI must balance the stack")
        self.assertEqual(run.mpu.a, IRQ_ENTRY_A, "A must be restored")
        assert run.memory.access is not None
        self.assertTrue(run.memory.access[CIA2.ICR], "the NMI must be acked at $DD0D")
        self.assertLessEqual(
            written_addresses(run),
            {0xD418, READ_PTR_LO_ADDR, READ_PTR_HI_ADDR, 0x01FC},
            "the routine stores only the sample, its own read pointer and its PHA",
        )

    def test_bring_up_uploads_the_routine_these_tests_run(self):
        s = _new_streamer()
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(cast(FakeAPI, s.api).mem_files[f"{NMI_ROUTINE_ADDR:04X}"], NMI_ROUTINE)

    def test_plays_the_sample_at_r_and_advances_r(self):
        r = RING_BUFFER_ADDR + 0x123
        run = self._run(r, sample=0x0B)
        self.assertEqual(run.memory.ram[0xD418], 0x0B)
        self.assertEqual(self._r(run), r + 1)
        self._assert_clean_return(run)

    def test_r_carries_into_the_next_page(self):
        r = RING_BUFFER_ADDR + 0x1FF
        run = self._run(r)
        self.assertEqual(self._r(run), r + 1)
        self._assert_clean_return(run)

    def test_r_wraps_from_the_last_ring_byte_to_the_first(self):
        run = self._run(RING_BUFFER_END - 1, sample=0x0E)
        self.assertEqual(run.memory.ram[0xD418], 0x0E)
        self.assertEqual(self._r(run), RING_BUFFER_ADDR)
        self._assert_clean_return(run)


class ReuIrqHandlerTest(unittest.TestCase):
    """The IRQ handler is hand-assembled bytes — a typo here can JAM the
    CPU at runtime (KIL opcodes silently halt the 6502). Verify length and
    that the BCC branch lands on a valid instruction boundary."""

    def test_handler_length_is_known(self):
        # If the handler grows or shrinks, the BCC offset (currently +10) may
        # need recomputation to reach the trailing PLA. The audio module also
        # asserts this length at import time.
        self.assertEqual(len(REU_IRQ_HANDLER), 37)

    def test_bcc_lands_on_pla(self):
        # The BCC byte pair is at offset 21-22 (after PHA / length-reset /
        # LDA #$91 / STA $DF01 / LDA $DF03 / CMP #$A0). The +10 displacement
        # from post-branch PC (=23) targets offset 33 (the PLA). Verify
        # those exact bytes — a wrong displacement landed in the middle of
        # STA $DF02 during dev and silently JAMmed the CPU at runtime.
        self.assertEqual(REU_IRQ_HANDLER[21], 0x90)  # BCC opcode
        self.assertEqual(REU_IRQ_HANDLER[22], 0x0A)  # +10 displacement
        self.assertEqual(REU_IRQ_HANDLER[33], 0x68)  # PLA at branch target

    def test_handler_ends_in_jmp_kernal_irq(self):
        # Last 3 bytes must be JMP $EA31 (chain to kernal IRQ for keyboard
        # scan, jiffy clock, etc.). Without this, $028D wouldn't update and
        # the Commodore-key poller would stop seeing pause/skip events.
        self.assertEqual(REU_IRQ_HANDLER[-3:], bytes([0x4C, 0x31, 0xEA]))


class StartForReuStagedTest(unittest.TestCase):
    """Verify the bring-up sequence for the REU pump."""

    def test_empty_audio_is_noop(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        # The no-op path logs an expected warning; assertLogs asserts it and
        # keeps it off the console.
        with self.assertLogs("c64cast.audio.audio", level="WARNING"):
            s.start_for_reu_staged(b"")
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertFalse(s._reu_pump_armed)

    def test_a_held_start_leaves_the_nmi_off_and_the_clock_at_zero(self):
        s = _new_streamer()
        with mock.patch.object(s.nmi, "start") as nmi_start:
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, hold=True)
        self.addCleanup(s.stop)
        nmi_start.assert_not_called()
        self.assertEqual(s.position_seconds(), 0.0)
        self.assertIsNotNone(s._pending_arm)

    def test_release_arms_the_pump_and_starts_the_clock(self):
        s = _new_streamer()
        with mock.patch.object(s.nmi, "start") as nmi_start:
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, hold=True)
            self.addCleanup(s.stop)
            s.release_hold()
        nmi_start.assert_called_once()
        self.assertIsNone(s._pending_arm)
        self.assertGreater(s._reu_pump_start_time, 0.0)

    def test_stop_while_held_drops_the_arm(self):
        s = _new_streamer()
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, hold=True)
        s.stop()
        self.assertIsNone(s._pending_arm)
        s.release_hold()
        self.assertFalse(s.running)

    def test_reu_upload_is_chunked_into_slices(self):
        """A 100 KB audio blob should arrive as ceil(100K / 32K) = 4
        REUWRITEs covering offsets 0, 32K, 64K, 96K, followed by EOF-pad
        writes (NEUTRAL_SAMPLE for ~5 sec to prevent garbage hiss after
        the pump runs past source end)."""
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        audio = b"\x07" * (100 * 1024)
        s.start_for_reu_staged(audio)
        offsets = [off for off, _ in fake.socket_dma.reuwrites]
        # First 4 writes are the source itself.
        self.assertEqual(
            offsets[:4], [0, REU_UPLOAD_SLICE, 2 * REU_UPLOAD_SLICE, 3 * REU_UPLOAD_SLICE]
        )
        # Source bytes are exactly preserved across the first 4 writes.
        source_bytes = b"".join(d for _, d in fake.socket_dma.reuwrites[:4])
        self.assertEqual(source_bytes, audio)
        # Subsequent writes are EOF padding (NEUTRAL_SAMPLE bytes), starting
        # right after the source ends.
        pad_writes = fake.socket_dma.reuwrites[4:]
        self.assertGreater(len(pad_writes), 0, "expected EOF pad writes")
        first_pad_off, first_pad_data = pad_writes[0]
        self.assertEqual(first_pad_off, len(audio))
        # Pad payload is all NEUTRAL_SAMPLE.
        for _, data in pad_writes:
            self.assertTrue(
                all(b == NEUTRAL_SAMPLE for b in data), "EOF pad must be all NEUTRAL_SAMPLE"
            )

    def test_upload_progress_is_monotonic_and_ends_at_one(self):
        """on_progress tracks payload + EOF-pad bytes: called at least once
        per payload slice, never regresses, and the final call is exactly
        1.0 (the setup bar relies on the endpoint)."""
        s = _new_streamer()
        audio = b"\x07" * (100 * 1024)
        fractions: list[float] = []
        s.start_for_reu_staged(audio, on_progress=fractions.append)
        self.assertGreaterEqual(len(fractions), -(-len(audio) // REU_UPLOAD_SLICE))
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)
        self.assertTrue(all(0.0 < f <= 1.0 for f in fractions))

    def test_handler_lands_at_c100(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        audio = b"\x07" * (RING_BUFFER_SIZE + 1024)
        s.start_for_reu_staged(audio)
        # The 37-byte IRQ handler is uploaded to $C100 via write_memory_file.
        # Find the entry with that address — case-insensitive hex key.
        key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(fake.mem_files[key], REU_IRQ_HANDLER)

    def test_ring_is_prefilled_with_first_bytes_of_audio(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        audio = bytes(range(256)) * 64  # 16384 bytes, distinct pattern
        s.start_for_reu_staged(audio)
        ring_key = f"{RING_BUFFER_ADDR:04X}"
        # The pre-fill of the ring is the LAST write to $8000 (after the
        # initial NEUTRAL fill from _upload_nmi_and_buffers).
        ring_writes = [b for k, b in fake.writes if k == ring_key]
        self.assertGreaterEqual(len(ring_writes), 2, "expected NEUTRAL fill THEN audio prefill")
        self.assertEqual(ring_writes[-1], audio[:RING_BUFFER_SIZE])

    def test_short_audio_is_padded_to_ring_size(self):
        """If audio is shorter than the ring, prefill should pad the tail
        with NEUTRAL_SAMPLE so NMI doesn't read undefined RAM."""
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        audio = bytes([0xAA] * 1024)
        s.start_for_reu_staged(audio)
        ring_key = f"{RING_BUFFER_ADDR:04X}"
        ring_writes = [b for k, b in fake.writes if k == ring_key]
        prefill = ring_writes[-1]
        self.assertEqual(len(prefill), RING_BUFFER_SIZE)
        self.assertEqual(prefill[:1024], audio)
        self.assertEqual(prefill[1024:], bytes([NEUTRAL_SAMPLE] * (RING_BUFFER_SIZE - 1024)))

    def test_cia1_latch_is_reprogrammed(self):
        """The CIA #1 Timer A latch must be the matched pump period — chunk x
        the NMI period — so the pump produces exactly what NMI consumes."""
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        # $DC04 LO + HI written as a 4-hex-char packed value.
        self.assertIn("DC04", fake.memories)
        self.assertEqual(fake.memories["DC04"], _packed_latch(MATCHED_LATCH_12KHZ))
        self.assertEqual(s._reu_cia1_latch_nominal, MATCHED_LATCH_12KHZ)

    def test_irq_vector_patched_last(self):
        """The IRQ vector must be the LAST significant write — patching it
        first would have the kernal IRQ fire into a handler before the REU
        registers are set up, causing garbage transfers."""
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        # Find the IRQ vector write (using write_regs at $0314) and check
        # it comes after the REU register init ($DF02, $DF04, etc.).
        # write_regs stores under the base key; the vector patch is at $0314.
        self.assertIn("0314", fake.regs)
        self.assertEqual(
            fake.regs["0314"], (REU_PUMP_HANDLER_ADDR & 0xFF, (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF)
        )

    def test_reu_pump_armed_state_is_set(self):
        s = _new_streamer()
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertTrue(s.running)
        self.assertTrue(s._reu_pump_armed)


class ReuPumpChunkSizeOverrideTest(unittest.TestCase):
    """When the caller passes chunk_size, both the handler bytes and the
    CIA #1 latch must reflect the override — otherwise the pump rate and
    the per-IRQ DMA size are mismatched and the ring oscillates."""

    def test_default_chunk_uses_module_constant(self):
        from c64cast.audio.audio_handlers import REU_PUMP_CHUNK_SIZE

        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        # Handler bytes at $C100 must have chunk_size baked in at the named
        # offsets the module publishes beside the assembly.
        key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        handler = next(b for k, b in fake.writes if k == key)
        lo, hi = REU_IRQ_HANDLER_CHUNK_OFFSETS
        self.assertEqual(handler[lo], REU_PUMP_CHUNK_SIZE & 0xFF)
        self.assertEqual(handler[hi], (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF)
        # CIA #1 latch = chunk x NMI period, at the default chunk of 128.
        self.assertEqual(fake.memories["DC04"], _packed_latch(MATCHED_LATCH_12KHZ))

    def test_custom_chunk_patches_handler_and_latch(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, chunk_size=64)
        key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        handler = next(b for k, b in fake.writes if k == key)
        self.assertEqual(handler[2], 64)
        self.assertEqual(handler[7], 0)
        # Latch = chunk x NMI period = 64 x 85 - 1 = 5439.
        self.assertEqual(fake.memories["DC04"], _packed_latch(64 * 85 - 1))
        # And $DF07 = chunk LO/HI for initial REC length.
        self.assertEqual(fake.memories["DF07"], "4000")


class ReuPumpInitialMarginTest(unittest.TestCase):
    """The pump's write pointer must start half a ring BEHIND the reader
    (REU_PUMP_INITIAL_MARGIN) so timing jitter has ~0.5 s of headroom before
    read/write cross and produce the stale-data echo. Both the plain
    auto-increment path (initial $DF02/$DF04 regs) and the tracked path
    (seeded $C200 tracker) must seed the same half-ring offset, and src
    offset ≡ dst position (mod ring) so the sample→position mapping holds."""

    def test_margin_is_half_ring(self):
        self.assertEqual(REU_PUMP_INITIAL_MARGIN, RING_BUFFER_SIZE // 2)

    def test_plain_path_seeds_half_ring_dst_and_src(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        # $DF02 (C64_ADDR_LO) = dest = ring start + margin = $5000 → "0050".
        dst = RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN
        self.assertEqual(fake.memories["DF02"], f"{dst & 0xFF:02X}{(dst >> 8) & 0xFF:02X}")
        # $DF04 (REU_ADDR_LO) = src 24-bit = REU base + margin = $1000.
        src = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        self.assertEqual(
            fake.memories["DF04"],
            f"{src & 0xFF:02X}{(src >> 8) & 0xFF:02X}{(src >> 16) & 0xFF:02X}",
        )

    def test_src_offset_congruent_to_dst_position(self):
        # Data continuity invariant: REU sample N must land at ring position
        # (N mod ring). That holds iff initial src offset ≡ (dst − ring base)
        # (mod ring) — both equal REU_PUMP_INITIAL_MARGIN here.
        src = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        dst_pos = (RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN) - RING_BUFFER_ADDR
        self.assertEqual(src % RING_BUFFER_SIZE, dst_pos % RING_BUFFER_SIZE)

    def test_tracked_path_seeds_half_ring_in_tracker(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        # Tracker at $C200 = src LO/MI/HI then dst LO/HI, all at half-ring.
        src = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        dst = RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN
        expected = (
            f"{src & 0xFF:02X}{(src >> 8) & 0xFF:02X}{(src >> 16) & 0xFF:02X}"
            f"{dst & 0xFF:02X}{(dst >> 8) & 0xFF:02X}"
        )
        self.assertEqual(fake.memories[f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}"], expected)


class ReuStopTeardownTest(unittest.TestCase):
    def test_stop_restores_irq_vector_when_pump_armed(self):
        """After stop(), $0314 must point back at the kernal handler ($EA31)
        so the next scene's kernal IRQ continues to work cleanly."""
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(
            fake.regs["0314"], (REU_PUMP_HANDLER_ADDR & 0xFF, (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF)
        )
        s.stop()
        # After stop: $0314 points at $EA31 (kernal IRQ handler).
        self.assertEqual(fake.regs["0314"], (0x31, 0xEA))
        self.assertFalse(s._reu_pump_armed)

    def test_stop_is_idempotent_in_reu_mode(self):
        s = _new_streamer()
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        s.stop()
        # The REU pump state has already been torn down.
        s.stop()
        self.assertFalse(s._reu_pump_armed)

    def test_stop_without_reu_pump_is_safe(self):
        """If REU pump was never armed, _disarm_reu_pump should be a no-op
        and stop() should not write to the IRQ vector at all (we don't
        want to clobber whatever the host-DMA path may have set)."""
        s = _new_streamer(use_reu_pump=False)
        fake = cast(FakeAPI, s.api)
        # Don't call start_for_reu_staged. Just call stop().
        s.stop()
        self.assertNotIn("0314", fake.regs)


class StartForReuStagedSkipVectorHookTest(unittest.TestCase):
    """When the display mode's bank-swap dispatcher already owns $0314 and
    JMPs to $C100 on non-raster IRQs, the audio install must NOT re-hook
    $0314 (that would clobber the dispatcher). Everything else — handler
    bytes at $C100, CIA #1 latch, REU regs, NMI bring-up — must still
    happen."""

    def test_default_hook_is_set(self):
        # Sanity: without the override, $0314 IS patched (existing
        # behavior preserved).
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertIn("0314", fake.regs)

    def test_skip_hook_leaves_vector_alone(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertNotIn("0314", fake.regs)

    def test_skip_hook_uploads_tracked_handler(self):
        # skip_irq_vector_hook implies the bank-swap dispatcher owns
        # $0314 and stomps REC between audio IRQs. The audio handler at
        # $C100 must be the TRACKED variant that reloads $DF04-$DF06 from
        # the main-RAM tracker every IRQ, not the plain auto-increment one.
        from c64cast.audio.audio_handlers import REU_AUDIO_SRC_TRACKER_ADDR, REU_IRQ_HANDLER_TRACKED

        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertIn(f"{REU_PUMP_HANDLER_ADDR:04X}", fake.mem_files)
        uploaded = fake.mem_files[f"{REU_PUMP_HANDLER_ADDR:04X}"]
        # Same length as the tracked variant; only chunk-size patches differ.
        self.assertEqual(len(uploaded), len(REU_IRQ_HANDLER_TRACKED))
        self.assertIn(f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}", fake.memories)

    def test_default_hook_uploads_plain_handler(self):
        # Inverse: the solo audio path (no merged dispatcher) keeps the plain
        # handler.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(len(fake.mem_files[f"{REU_PUMP_HANDLER_ADDR:04X}"]), len(REU_IRQ_HANDLER))

    def test_skip_hook_still_reprograms_cia1_latch(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertIn("DC04", fake.memories)

    def test_skip_hook_still_arms_pump_state(self):
        s = _new_streamer()
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertTrue(s._reu_pump_armed)
        self.assertTrue(s.running)

    def test_skip_hook_uploads_pump_body_subroutine(self):
        # The chunked mhires bank-swap dispatcher JSRs to $C180 between
        # families (audio.REU_PUMP_BODY_SUBROUTINE_ADDR). Without the
        # body bytes there, the JSR returns from uninitialized RAM.
        # Verify both the body bytes and the address are uploaded.
        from c64cast.audio.audio_handlers import (
            REU_PUMP_BODY_SUBROUTINE,
            REU_PUMP_BODY_SUBROUTINE_ADDR,
        )

        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        key = f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(fake.mem_files[key], REU_PUMP_BODY_SUBROUTINE)

    def test_skip_hook_uploads_body_before_entry(self):
        # The pump body must be in place BEFORE the $C100 entry replaces the
        # JMP $EA31 stub the bank-swap installer left there: a CIA #1 IRQ firing
        # between the entry write and the body write would JSR into
        # uninitialized RAM.
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_ADDR

        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        body_key = f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}"
        entry_key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        body_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_memory_file" and op[1].upper() == body_key
        )
        entry_idx = next(
            i
            for i, op in enumerate(fake.ops)
            if op[0] == "write_memory_file" and op[1].upper() == entry_key
        )
        self.assertLess(body_idx, entry_idx)


def _tracker_seed(src: int, dst: int) -> dict[int, int]:
    """The $C200 tracker bytes for a 24-bit REU ``src`` and a ring ``dst``."""
    t = REU_AUDIO_SRC_TRACKER_ADDR
    return {
        t + 0: src & 0xFF,
        t + 1: (src >> 8) & 0xFF,
        t + 2: (src >> 16) & 0xFF,
        t + 3: dst & 0xFF,
        t + 4: (dst >> 8) & 0xFF,
    }


def _jsr_tracked_governor(test: unittest.TestCase, seed: dict[int, int]):
    """Run REU_PUMP_BODY_SUBROUTINE_GOVERNOR through a JSR / JMP $EA31 caller,
    the way both of its callers reach it, and check it returned balanced."""
    from c64cast.audio.audio_handlers import (
        REU_PUMP_BODY_SUBROUTINE_ADDR,
        REU_PUMP_BODY_SUBROUTINE_GOVERNOR,
    )

    caller = bytes(
        [
            0x20,
            REU_PUMP_BODY_SUBROUTINE_ADDR & 0xFF,
            REU_PUMP_BODY_SUBROUTINE_ADDR >> 8,  # JSR $C180
            0x4C,
            0x31,
            0xEA,  # JMP $EA31
        ]
    )
    run = run_irq_handler(
        caller,
        addr=0xC000,
        seed=seed,
        images={REU_PUMP_BODY_SUBROUTINE_ADDR: REU_PUMP_BODY_SUBROUTINE_GOVERNOR},
    )
    test.assertEqual(run.exit_pc, 0xEA31, "the subroutine must RTS to its caller")
    test.assertEqual(run.mpu.sp, 0xFF)
    return run


class ReuTrackedHandlerTest(unittest.TestCase):
    """REU_IRQ_HANDLER_TRACKED, EXECUTED on the repo's own 6502 (see
    _fakes.run_irq_handler) instead of pinning instruction offsets: the
    old tests carried the offset in the test NAME (test_pla_at_offset_105),
    so inserting one instruction broke all six and required renames —
    while the constraint they guard (a wrong branch displacement JAMs the
    CPU) is exactly what actually running the handler proves.

    Covers the tick-divider / lean-exit pattern (borrowed from the SID
    player: chain to $EA31 every Nth CIA #1 tick, ack + JMP $EA81 for a
    lean RTI the other N-1), the tracker-driven REC pump the entry JSRs at
    $C180, and the dst ring wrap. Runs the open-loop body; the governed
    one has its own class below."""

    def _run(self, *, counter: int, dst: int | None = None, src: int = 0x032211):
        from c64cast.audio.audio_handlers import (
            REU_IRQ_HANDLER_TRACKED,
            REU_PUMP_BODY_SUBROUTINE,
            REU_PUMP_BODY_SUBROUTINE_ADDR,
            REU_PUMP_TICK_COUNTER_ADDR,
        )

        dst = RING_BUFFER_ADDR if dst is None else dst
        seed = _tracker_seed(src, dst)
        seed[REU_PUMP_TICK_COUNTER_ADDR] = counter
        run = run_irq_handler(
            REU_IRQ_HANDLER_TRACKED,
            addr=REU_PUMP_HANDLER_ADDR,
            seed=seed,
            images={REU_PUMP_BODY_SUBROUTINE_ADDR: REU_PUMP_BODY_SUBROUTINE},
        )
        run.src, run.dst = src, dst
        return run

    def test_on_cycle_tick_chains_and_reloads_the_divider(self):
        from c64cast.audio.audio_handlers import (
            REU_PUMP_TICK_COUNTER_ADDR,
            REU_PUMP_TICK_DIVIDER,
        )

        run = self._run(counter=1)
        self.assertEqual(run.exit_pc, 0xEA31, "Nth tick must chain the full kernal tail")
        self.assertEqual(run.memory.ram[REU_PUMP_TICK_COUNTER_ADDR], REU_PUMP_TICK_DIVIDER)
        self.assertEqual(run.mpu.sp, 0xFF, "handler must balance its own PHA/PLA")

    def test_off_cycle_tick_takes_the_lean_exit_and_acks_cia(self):
        from c64cast.audio.audio_handlers import (
            REU_PUMP_TICK_COUNTER_ADDR,
            REU_PUMP_TICK_DIVIDER,
        )
        from c64cast.hw.c64 import CIA1

        run = self._run(counter=REU_PUMP_TICK_DIVIDER)
        self.assertEqual(run.exit_pc, 0xEA81, "off-cycle ticks must take the lean exit")
        self.assertEqual(run.memory.ram[REU_PUMP_TICK_COUNTER_ADDR], REU_PUMP_TICK_DIVIDER - 1)
        assert run.memory.access is not None
        self.assertTrue(run.memory.access[CIA1.ICR], "lean exit must still ack CIA #1")
        self.assertEqual(run.mpu.sp, 0xFF)

    def test_every_tick_pumps_one_chunk_from_the_trackers(self):
        from c64cast.audio.audio_handlers import REU_CMD_FETCH_EXEC

        run = self._run(counter=2)  # off-cycle — the pump body runs on EVERY tick
        ram = run.memory.ram
        # REC programmed from the main-RAM trackers (never from register
        # read-back) and triggered:
        self.assertEqual(ram[0xDF07], REU_PUMP_CHUNK_SIZE & 0xFF)
        self.assertEqual(ram[0xDF08], (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF)
        self.assertEqual(
            [ram[0xDF04], ram[0xDF05], ram[0xDF06]],
            [run.src & 0xFF, (run.src >> 8) & 0xFF, (run.src >> 16) & 0xFF],
        )
        self.assertEqual([ram[0xDF02], ram[0xDF03]], [run.dst & 0xFF, run.dst >> 8])
        self.assertEqual(ram[0xDF01], REU_CMD_FETCH_EXEC)
        # Both trackers advanced one chunk for the next tick.
        t = REU_AUDIO_SRC_TRACKER_ADDR
        src_after = ram[t] | (ram[t + 1] << 8) | (ram[t + 2] << 16)
        self.assertEqual(src_after, run.src + REU_PUMP_CHUNK_SIZE)
        dst_after = ram[t + 3] | (ram[t + 4] << 8)
        self.assertEqual(dst_after, run.dst + REU_PUMP_CHUNK_SIZE)

    def test_src_carries_across_a_64k_reu_boundary(self):
        # A staged track is megabytes long, so its src tracker crosses a 64 KB
        # bank every ~5.4 s at 12 kHz. A carry that stopped at the MI byte would
        # replay the first bank forever.
        src = 0x01FFFF - REU_PUMP_CHUNK_SIZE + 1
        run = self._run(counter=2, src=src)
        ram = run.memory.ram
        self.assertEqual([ram[0xDF04], ram[0xDF05], ram[0xDF06]], [0x80, 0xFF, 0x01])
        t = REU_AUDIO_SRC_TRACKER_ADDR
        self.assertEqual(ram[t] | (ram[t + 1] << 8) | (ram[t + 2] << 16), 0x020000)

    def test_stores_only_to_the_rec_the_trackers_the_counter_and_the_stack(self):
        from c64cast.audio.audio_handlers import REU_PUMP_TICK_COUNTER_ADDR

        t = REU_AUDIO_SRC_TRACKER_ADDR
        allowed = (
            set(range(0xDF01, 0xDF09))
            | set(range(t, t + 5))
            | {REU_PUMP_TICK_COUNTER_ADDR}
            | set(range(0x0100, 0x0200))
        )
        for counter in (1, 2):
            for dst in (RING_BUFFER_ADDR, RING_BUFFER_END - REU_PUMP_CHUNK_SIZE):
                with self.subTest(counter=counter, dst=dst):
                    run = self._run(counter=counter, dst=dst, src=0x01FF80)
                    self.assertLessEqual(written_addresses(run), allowed)

    def test_dst_tracker_wraps_at_ring_end(self):
        # The last chunk before the ring end must wrap the dst tracker back
        # to the ring start (not $DF03 — that register goes stale whenever
        # the bank-swap pipeline ran between IRQs).
        last_chunk_dst = (RING_BUFFER_END_HI << 8) - REU_PUMP_CHUNK_SIZE
        run = self._run(counter=2, dst=last_chunk_dst)
        t = REU_AUDIO_SRC_TRACKER_ADDR
        self.assertEqual(run.memory.ram[t + 3], 0x00)
        self.assertEqual(run.memory.ram[t + 4], RING_BUFFER_HI)

    def test_skip_hook_seeds_tick_counter_to_one(self):
        # First IRQ must DEC the counter to 0, trigger reload+chain, then
        # N-1 lean exits follow. Seeding to 1 guarantees that on-cycle
        # right from the start regardless of what byte was at $C205.
        from c64cast.audio.audio_handlers import REU_PUMP_TICK_COUNTER_ADDR

        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertIn(f"{REU_PUMP_TICK_COUNTER_ADDR:04X}", fake.memories)
        self.assertEqual(fake.memories[f"{REU_PUMP_TICK_COUNTER_ADDR:04X}"], "01")


class ReuTrackedGovernorTest(unittest.TestCase):
    """REU_PUMP_BODY_SUBROUTINE_GOVERNOR, executed through a JSR the way both
    of its callers reach it (the $C100 entry and the chunked mhires
    dispatcher). Under the bank-swap video DMAs the open-loop tracked pump
    lapped the NMI reader every 10.5-12 s (#544); the governed one must skip
    a chunk while its write head is half a ring ahead of R, pump otherwise
    (GovernorSkipWindowTest sweeps every gap), and return to its caller on
    both paths."""

    SRC = 0x032211

    def _call(self, *, dst: int, r: int, df03: int | None = None):
        seed = _tracker_seed(self.SRC, dst)
        seed[READ_PTR_HI_ADDR] = r >> 8
        if df03 is not None:
            seed[0xDF03] = df03
        return _jsr_tracked_governor(self, seed)

    def _pumped(self, run) -> bool:
        from c64cast.audio.audio_handlers import REU_CMD_FETCH_EXEC

        t = REU_AUDIO_SRC_TRACKER_ADDR
        ram = run.memory.ram
        src_after = ram[t] | (ram[t + 1] << 8) | (ram[t + 2] << 16)
        triggered = ram[0xDF01] == REU_CMD_FETCH_EXEC
        self.assertEqual(
            triggered,
            src_after == self.SRC + REU_PUMP_CHUNK_SIZE,
            "a trigger and a tracker advance must go together",
        )
        return triggered

    def test_skips_when_write_head_is_half_a_ring_ahead(self):
        r = RING_BUFFER_ADDR
        run = self._call(dst=r + REU_PUMP_INITIAL_MARGIN, r=r)
        self.assertFalse(self._pumped(run))
        t = REU_AUDIO_SRC_TRACKER_ADDR
        dst_after = run.memory.ram[t + 3] | (run.memory.ram[t + 4] << 8)
        self.assertEqual(dst_after, r + REU_PUMP_INITIAL_MARGIN, "a skip must not advance dst")

    def test_pumps_once_the_reader_has_overtaken_the_write_head(self):
        # W one page behind R reads as 31 pages ahead mod the ring, but no
        # pump can carry W past 16 pages ahead: only R overtaking W reaches
        # it. Skipping there would stall W for half a ring of lap-old audio
        # and leave the source behind the picture (#544).
        r = RING_BUFFER_ADDR + 0x1000
        self.assertTrue(self._pumped(self._call(dst=r - 0x100, r=r)))

    def test_pumps_when_write_head_is_less_than_half_a_ring_ahead(self):
        r = RING_BUFFER_ADDR
        run = self._call(dst=r + REU_PUMP_INITIAL_MARGIN - 0x100, r=r)
        self.assertTrue(self._pumped(run))

    def test_pumps_across_the_ring_wrap(self):
        # W near the ring's start, R near its end: W is a few pages ahead
        # once the wrap is accounted for, so it must pump.
        r = RING_BUFFER_END - 0x100
        self.assertTrue(self._pumped(self._call(dst=RING_BUFFER_ADDR + 0x200, r=r)))

    def test_reads_the_write_head_from_the_tracker_not_the_reu_register(self):
        # The bank-swap DMAs leave $DF03 pointing into video memory, so a
        # governor that read it would decide on garbage. Seed $DF03 with a
        # value that says "far ahead" while the tracker says "close behind".
        r = RING_BUFFER_ADDR
        run = self._call(dst=r + 0x100, r=r, df03=(r + REU_PUMP_INITIAL_MARGIN) >> 8)
        self.assertTrue(self._pumped(run))

    def test_governed_body_is_the_open_loop_body_behind_the_test(self):
        from c64cast.audio.audio_handlers import (
            REU_PUMP_BODY_SUBROUTINE,
            REU_PUMP_BODY_SUBROUTINE_GOVERNOR,
        )

        self.assertTrue(REU_PUMP_BODY_SUBROUTINE_GOVERNOR.endswith(REU_PUMP_BODY_SUBROUTINE))


class TrackedPumpSelectionTest(unittest.TestCase):
    """start_for_reu_staged on the bank-swap path uploads the governed or the
    open-loop pump body at $C180 per reu_pump_governor, with the scene's chunk
    size patched in: the chunked mhires dispatcher calls that body directly,
    so an unpatched one pumps the default chunk on every call it makes."""

    def _body(self, *, governor: bool, chunk: int | None = None) -> bytes:
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_ADDR

        s = _new_streamer()
        s.reu_pump_governor = governor
        s.start_for_reu_staged(
            b"\x07" * RING_BUFFER_SIZE, chunk_size=chunk, skip_irq_vector_hook=True
        )
        return cast(FakeAPI, s.api).mem_files[f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}"]

    def test_governor_on_uploads_the_governed_body(self):
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_GOVERNOR

        self.assertEqual(self._body(governor=True), REU_PUMP_BODY_SUBROUTINE_GOVERNOR)

    def test_governor_off_uploads_the_open_loop_body(self):
        from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE

        self.assertEqual(self._body(governor=False), REU_PUMP_BODY_SUBROUTINE)

    def test_scene_chunk_is_patched_into_the_body(self):
        from c64cast.audio.audio_handlers import (
            REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS,
            REU_PUMP_BODY_SUBROUTINE_GOVERNOR_CHUNK_OFFSETS,
            REU_PUMP_CHUNK_SIZE_HEAVY_BUS,
        )

        chunk = REU_PUMP_CHUNK_SIZE_HEAVY_BUS
        for governor, offsets in (
            (True, REU_PUMP_BODY_SUBROUTINE_GOVERNOR_CHUNK_OFFSETS),
            (False, REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS),
        ):
            with self.subTest(governor=governor):
                body = self._body(governor=governor, chunk=chunk)
                self.assertEqual(
                    [body[off] for off in offsets],
                    [(chunk >> (0 if i % 2 == 0 else 8)) & 0xFF for i in range(len(offsets))],
                )


class PumpChunkTilesRingTest(unittest.TestCase):
    """A pump chunk that does not divide the 8 KB ring DMAs past
    RING_BUFFER_END once per lap; the wrap then resets dst to the ring start
    and the overshoot is never played (80 dropped 48-79 samples a lap). Both
    shipped chunks have to tile the ring, and a caller's chunk_size that does
    not is refused."""

    LAPS = 3

    def _pump_laps(self, chunk: int, *, tracked: bool) -> list[tuple[int, int]]:
        """(dst, src) of every DMA over LAPS ring laps of the open-loop pump,
        run on py65 from start_for_reu_staged's seeded pointers.

        The tracked pump reloads both from the $C200 tracker, so the DMA is
        what it wrote to the REU registers. The plain handler relies on the
        REU's own auto-increment, which py65 does not model: this loop keeps
        the DMA's pointers and seeds $DF02/$DF03 with their post-transfer
        value, which is what the handler's wrap check reads."""
        from c64cast.audio.audio_handlers import (
            REU_IRQ_HANDLER_TRACKED,
            REU_PUMP_BODY_SUBROUTINE,
            REU_PUMP_BODY_SUBROUTINE_ADDR,
            REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS,
            REU_PUMP_TICK_COUNTER_ADDR,
        )

        src = REU_AUDIO_BASE + REU_PUMP_INITIAL_MARGIN
        dst = RING_BUFFER_ADDR + REU_PUMP_INITIAL_MARGIN
        if tracked:
            handler = REU_IRQ_HANDLER_TRACKED
            images = {
                REU_PUMP_BODY_SUBROUTINE_ADDR: patch_chunk_size(
                    REU_PUMP_BODY_SUBROUTINE, REU_PUMP_BODY_SUBROUTINE_CHUNK_OFFSETS, chunk
                )
            }
        else:
            handler = patch_chunk_size(REU_IRQ_HANDLER, REU_IRQ_HANDLER_CHUNK_OFFSETS, chunk)
            images = {}
        tracker = _tracker_seed(src, dst)
        dmas = []
        for _ in range(self.LAPS * RING_BUFFER_SIZE // chunk + 1):
            seed = {**tracker, REU_PUMP_TICK_COUNTER_ADDR: 2}
            if not tracked:
                after = dst + chunk
                seed.update({0xDF02: after & 0xFF, 0xDF03: after >> 8})
            run = run_irq_handler(handler, addr=REU_PUMP_HANDLER_ADDR, seed=seed, images=images)
            ram = run.memory.ram
            if tracked:
                dmas.append(
                    (
                        ram[0xDF02] | (ram[0xDF03] << 8),
                        ram[0xDF04] | (ram[0xDF05] << 8) | (ram[0xDF06] << 16),
                    )
                )
                tracker = {a: ram[a] for a in tracker}
            else:
                dmas.append((dst, src))
                src += chunk
                dst = ram[0xDF02] | (ram[0xDF03] << 8)
        return dmas

    def test_shipped_chunks_never_dma_past_the_ring_and_keep_the_mapping(self):
        from c64cast.audio.audio_handlers import REU_PUMP_CHUNK_SIZE_HEAVY_BUS

        for chunk in (REU_PUMP_CHUNK_SIZE, REU_PUMP_CHUNK_SIZE_HEAVY_BUS):
            for tracked in (True, False):
                with self.subTest(chunk=chunk, tracked=tracked):
                    dmas = self._pump_laps(chunk, tracked=tracked)
                    self.assertGreaterEqual(len(dmas), self.LAPS * RING_BUFFER_SIZE // chunk)
                    self.assertEqual(
                        [d for d, _ in dmas if d + chunk > RING_BUFFER_END],
                        [],
                        "a pump DMA ran past RING_BUFFER_END",
                    )
                    self.assertEqual(
                        {(s - (d - RING_BUFFER_ADDR)) % RING_BUFFER_SIZE for d, s in dmas},
                        {(REU_AUDIO_BASE) % RING_BUFFER_SIZE},
                        "REU sample N must land at ring position N mod RING_BUFFER_SIZE",
                    )

    def test_start_refuses_a_chunk_that_does_not_tile_the_ring(self):
        # 80 does not divide the ring; RING_BUFFER_SIZE divides the ring but
        # not the half-ring margin the write head is seeded at.
        for chunk in (80, 0, RING_BUFFER_SIZE, RING_BUFFER_SIZE * 2):
            with self.subTest(chunk=chunk):
                s = _new_streamer()
                with self.assertRaises(ValueError):
                    s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, chunk_size=chunk)
                self.assertEqual(cast(FakeAPI, s.api).writes, [])
                self.assertEqual(cast(FakeAPI, s.api).socket_dma.reuwrites, [])


class ReuPositionSecondsTest(unittest.TestCase):
    """In REU mode, position_seconds is wall-clock based (no host queue
    to count). Verify the formula handles edge cases."""

    def test_zero_before_arm(self):
        s = _new_streamer()
        # Not yet armed — falls through to host-DMA formula which is 0
        # for a fresh streamer (pushed_count=0, queued_samples=0).
        self.assertEqual(s.position_seconds(), 0.0)

    def test_clamped_to_total_after_arm(self):
        """When wall-clock elapsed exceeds the audio length, position is
        clamped to the total length so video doesn't desync."""
        import time as time_mod

        s = _new_streamer()
        s._reu_pump_armed = True
        s._reu_pump_total_samples = round(s.effective_rate)  # one second of it
        s._reu_pump_start_time = time_mod.monotonic() - 100.0
        # Expected: min(100s, 1s) = 1.0
        self.assertAlmostEqual(s.position_seconds(), 1.0, places=2)

    def test_live_source_with_no_total_is_not_clamped(self):
        """A REU-*mic* session has no finite length and never sets a total, so
        the clamp must not apply: clamping to a zero total pinned the clock at
        0.0 for the whole session, and a total left over from a previous scene
        pinned it to the wrong track's length."""
        import time as time_mod

        s = _new_streamer()
        s._reu_pump_armed = True
        s._reu_pump_total_samples = 0
        s._reu_pump_start_time = time_mod.monotonic() - 5.0
        self.assertAlmostEqual(s.position_seconds(), 5.0, places=2)


class GovernorHandlerTest(unittest.TestCase):
    """The C64-side governor handler is the plain pump handler with a 22-byte
    skip-when-ahead prefix. A typo in the prefix bytes or branch displacement
    JAMs the 6502, so verify the structure (these run with no hardware)."""

    def test_length_is_prefix_plus_body(self):
        # 22-byte governor prefix + the 37-byte plain handler sans its PHA (36).
        self.assertEqual(len(REU_IRQ_HANDLER_GOVERNOR), 22 + 36)

    def test_starts_with_pha(self):
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[0], 0x48, "leading PHA")

    def test_reads_dst_hi_then_r_hi(self):
        # LDA $DF03 (dst HI), then SEC, then SBC $C026 (R HI).
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[1:4], bytes([0xAD, 0x03, 0xDF]), "LDA $DF03 (dst_hi)"
        )
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[4], 0x38, "SEC")
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[5:8],
            bytes([0xED, READ_PTR_HI_ADDR & 0xFF, (READ_PTR_HI_ADDR >> 8) & 0xFF]),
            "SBC $C026 (R_hi)",
        )

    def test_masks_gap_and_compares_the_skip_window(self):
        # AND #$1F masks gap to 5 bits (32 ring HI values + discards REU
        # read-back garbage); the two CMPs bound the skip window.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[8:10], bytes([0x29, 0x1F]), "AND #$1F")
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[10:12],
            bytes([0xC9, REU_GOVERNOR_GAP_THRESHOLD_HI]),
            "CMP #threshold_hi",
        )
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[14:16],
            bytes([0xC9, REU_GOVERNOR_OVERTAKE_GAP_HI]),
            "CMP #overtake_hi",
        )
        self.assertEqual(REU_GOVERNOR_GAP_THRESHOLD_HI, REU_PUMP_INITIAL_MARGIN >> 8)

    def test_branches_skip_over_skip_block_to_pump_body(self):
        # BCC +8 (offset 12) and BCS +4 (offset 16) both land on the pump body
        # at offset 22, past the 4-byte skip block (PLA + JMP $EA31). A wrong
        # displacement lands mid-JMP and JAMs the CPU.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[12:14], bytes([0x90, 0x08]), "BCC +8")
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[16:18], bytes([0xB0, 0x04]), "BCS +4")
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[18], 0x68, "skip-path PLA")
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[19:22], bytes([0x4C, 0x31, 0xEA]), "skip-path JMP $EA31"
        )
        # Pump body (offset 22) is REU_IRQ_HANDLER without its leading PHA.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[22:], REU_IRQ_HANDLER[1:])


class GovernorSkipWindowTest(unittest.TestCase):
    """Both governed pumps, EXECUTED on py65 at every ring gap: skip only for
    gap_hi in [REU_GOVERNOR_GAP_THRESHOLD_HI, REU_GOVERNOR_OVERTAKE_GAP_HI)
    (16-17), pump below it (0-15) and at or past it (18-31), where only the
    reader overtaking the write head can put the gap (#544)."""

    R = RING_BUFFER_ADDR + 0x0A40  # arbitrary, off a page boundary

    def _w(self, gap_hi: int) -> int:
        return RING_BUFFER_ADDR + ((self.R - RING_BUFFER_ADDR + (gap_hi << 8)) % RING_BUFFER_SIZE)

    def _plain_pumped(self, gap_hi: int) -> bool:
        from c64cast.audio.audio_handlers import REU_CMD_FETCH_EXEC

        w = self._w(gap_hi)
        run = run_irq_handler(
            REU_IRQ_HANDLER_GOVERNOR,
            addr=REU_PUMP_HANDLER_ADDR,
            seed={0xDF02: w & 0xFF, 0xDF03: w >> 8, READ_PTR_HI_ADDR: self.R >> 8},
        )
        self.assertEqual(run.exit_pc, 0xEA31)
        self.assertEqual(run.mpu.sp, 0xFF, "both paths must balance the PHA")
        return run.memory.ram[0xDF01] == REU_CMD_FETCH_EXEC

    def _tracked_pumped(self, gap_hi: int) -> bool:
        from c64cast.audio.audio_handlers import REU_CMD_FETCH_EXEC

        seed = _tracker_seed(0x032211, self._w(gap_hi))
        seed[READ_PTR_HI_ADDR] = self.R >> 8
        run = _jsr_tracked_governor(self, seed)
        return run.memory.ram[0xDF01] == REU_CMD_FETCH_EXEC

    def test_window_is_two_pages_above_half_a_ring(self):
        self.assertEqual((REU_GOVERNOR_GAP_THRESHOLD_HI, REU_GOVERNOR_OVERTAKE_GAP_HI), (16, 18))

    def test_both_governors_skip_only_inside_the_window(self):
        expected = {gap: not (16 <= gap < 18) for gap in range(32)}
        for name, pumped in (("plain", self._plain_pumped), ("tracked", self._tracked_pumped)):
            with self.subTest(governor=name):
                self.assertEqual({gap: pumped(gap) for gap in range(32)}, expected)

    def test_a_pump_cannot_carry_the_write_head_out_of_the_window(self):
        # The last gap that pumps below the window, plus one chunk of the
        # largest size the governor accepts, must still land below the
        # overtake bound — else the next tick reads an overtake and pumps on.
        from c64cast.audio.audio_handlers import REU_GOVERNOR_MAX_CHUNK

        last_pumping_gap_bytes = REU_GOVERNOR_GAP_THRESHOLD_HI << 8  # exclusive
        self.assertLess(
            (last_pumping_gap_bytes - 1 + REU_GOVERNOR_MAX_CHUNK) >> 8,
            REU_GOVERNOR_OVERTAKE_GAP_HI,
        )

    def test_start_refuses_a_governed_chunk_past_the_maximum(self):
        from c64cast.audio.audio_handlers import REU_GOVERNOR_MAX_CHUNK

        chunk = REU_GOVERNOR_MAX_CHUNK * 2  # tiles the ring, overshoots the window
        s = _new_streamer()
        s.reu_pump_governor = True
        with self.assertRaises(ValueError):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, chunk_size=chunk)
        self.assertEqual(cast(FakeAPI, s.api).writes, [])
        self.assertEqual(cast(FakeAPI, s.api).socket_dma.reuwrites, [])


class GovernorSelectionTest(unittest.TestCase):
    """start_for_reu_staged uploads the governor handler when reu_pump_governor
    is set (plain path), else the open-loop handler — with chunk patched at the
    right (prefix-shifted) offsets either way."""

    def _handler_at_c100(self, api: FakeAPI) -> bytes:
        # FakeAPI.write_memory_file records into mem_files (last-write-wins,
        # key = uppercase hex address).
        return api.mem_files[f"{REU_PUMP_HANDLER_ADDR:04X}"]

    def test_governor_handler_uploaded_when_enabled(self):
        s = _new_streamer()
        s.reu_pump_governor = True
        s.start_for_reu_staged(bytes([8] * 2048))
        handler = self._handler_at_c100(cast(FakeAPI, s.api))
        self.assertEqual(len(handler), len(REU_IRQ_HANDLER_GOVERNOR))
        self.assertEqual(handler[0], 0x48)
        self.assertEqual(handler[1:4], bytes([0xAD, 0x03, 0xDF]))  # governor prefix
        # chunk patched at the prefix-shifted offsets the module derives.
        lo, hi = REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS
        self.assertEqual((lo, hi), (23, 28))
        self.assertEqual(handler[lo], REU_PUMP_CHUNK_SIZE & 0xFF)
        self.assertEqual(handler[hi], (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF)

    def test_plain_handler_uploaded_when_disabled(self):
        s = _new_streamer()
        s.reu_pump_governor = False
        s.start_for_reu_staged(bytes([8] * 2048))
        handler = self._handler_at_c100(cast(FakeAPI, s.api))
        self.assertEqual(len(handler), len(REU_IRQ_HANDLER))
        self.assertEqual(handler[0], 0x48)
        self.assertEqual(handler[1], 0xA9)  # straight into LDA #<chunk
        self.assertEqual(handler[2], REU_PUMP_CHUNK_SIZE & 0xFF)


class HostDmaServoTest(unittest.TestCase):
    """The host-DMA pacing servo (servo_period PI controller + the
    _next_pace_increment read/guard wrapper). Pure math + a stubbed read, no
    threads or hardware — the controller was factored out specifically so this
    is testable without a U64."""

    CHUNK_PERIOD = 1024 / 8000.0  # 0.128 s, matches the worker default

    def test_at_target_gap_is_nominal(self):
        # Gap exactly at target, no accumulated history → no correction.
        period, integ = servo_period(HOST_DMA_SERVO_TARGET_GAP, 0.0, chunk_period=self.CHUNK_PERIOD)
        self.assertAlmostEqual(period, self.CHUNK_PERIOD)
        self.assertEqual(integ, 0.0)

    def test_ahead_lengthens_behind_shortens(self):
        # W too far ahead (gap > target) → slow down (longer period).
        ahead, _ = servo_period(
            HOST_DMA_SERVO_TARGET_GAP + 1000, 0.0, chunk_period=self.CHUNK_PERIOD
        )
        self.assertGreater(ahead, self.CHUNK_PERIOD)
        # W too close behind (gap < target) → speed up (shorter period).
        behind, _ = servo_period(
            HOST_DMA_SERVO_TARGET_GAP - 1000, 0.0, chunk_period=self.CHUNK_PERIOD
        )
        self.assertLess(behind, self.CHUNK_PERIOD)
        self.assertGreaterEqual(behind, HOST_DMA_SERVO_PERIOD_MIN_FRAC * self.CHUNK_PERIOD)

    def test_period_is_clamped(self):
        # Extreme errors saturate at [MIN, MAX]·chunk_period.
        hi, _ = servo_period(RING_BUFFER_SIZE - 1, 1e9, chunk_period=self.CHUNK_PERIOD)
        self.assertAlmostEqual(hi, HOST_DMA_SERVO_PERIOD_MAX_FRAC * self.CHUNK_PERIOD)
        lo, _ = servo_period(0, -1e9, chunk_period=self.CHUNK_PERIOD)
        self.assertAlmostEqual(lo, HOST_DMA_SERVO_PERIOD_MIN_FRAC * self.CHUNK_PERIOD)

    def test_integrator_anti_windup(self):
        # A large constant error for many iters must not let the integral's
        # contribution exceed INTEG_CLAMP·chunk_period.
        integ = 0.0
        for _ in range(10_000):
            _, integ = servo_period(RING_BUFFER_SIZE - 1, integ, chunk_period=self.CHUNK_PERIOD)
        from c64cast.audio.audio_servo import HOST_DMA_SERVO_KI

        self.assertLessEqual(
            abs(HOST_DMA_SERVO_KI * integ), HOST_DMA_SERVO_INTEG_CLAMP * self.CHUNK_PERIOD + 1e-12
        )

    def test_constant_drift_converges(self):
        # Closed-loop sim: R consumes at the measured ~7690 B/s while W advances
        # one chunk per returned period. Feed gap=(W-R)%ring back in and assert
        # the gap converges to ~target, the period settles to the rate-match
        # value, and the gap never laps (0) or underruns (ring).
        r_rate = 7690.0
        chunk = 1024
        ring = RING_BUFFER_SIZE
        # Start where the prebuffer leaves W: ~6 chunks (6144 B) ahead of R=0.
        w = 6144.0
        r = 0.0
        integ = 0.0
        period = self.CHUNK_PERIOD
        gaps = []
        for _ in range(400):
            gap = int(w - r) % ring
            gaps.append(gap)
            self.assertGreater(gap, 0)  # never lapped
            self.assertLess(gap, ring)  # never underran
            period, integ = servo_period(gap, integ, chunk_period=self.CHUNK_PERIOD)
            # Advance the model one chunk: W by chunk_size, R by its rate × the
            # (servo-chosen) elapsed period.
            w += chunk
            r += r_rate * period
        settled = gaps[-100:]
        mean_gap = sum(settled) / len(settled)
        self.assertLess(abs(mean_gap - HOST_DMA_SERVO_TARGET_GAP), 250)
        # Steady period should track chunk_size / r_rate (the rate match).
        self.assertAlmostEqual(period, chunk / r_rate, delta=0.002)

    def test_next_pace_increment_guards_bad_reads(self):
        # _next_pace_increment falls back to open-loop chunk_period when the
        # servo is off; holds the integral term (the bare chunk_period at
        # integ 0) when the read fails/short or R is out of the ring; and runs
        # the controller for an in-ring read.
        s = _new_streamer(use_reu_pump=False)
        write_addr = RING_BUFFER_ADDR + HOST_DMA_SERVO_TARGET_GAP + 1500

        # Servo off → always open-loop regardless of what R reads.
        s.host_dma_servo = False
        self.assertEqual(
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD), self.CHUNK_PERIOD
        )

        s.host_dma_servo = True
        cases = {
            None: self.CHUNK_PERIOD,  # read failed
            b"\x00": self.CHUNK_PERIOD,  # short read (len 1)
            bytes([0x00, 0x00]): self.CHUNK_PERIOD,  # $0000 out of ring
            bytes([0x00, 0x70]): self.CHUNK_PERIOD,  # $7000 out of ring
        }
        for ret, expect in cases.items():
            s.servo.integ = 0.0
            s.api.read_memory = lambda a, n, timeout=1.0, _r=ret: _r  # type: ignore[method-assign]
            self.assertEqual(s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD), expect)

        # A failed read keeps the learned bus-halt correction rather than
        # dropping to the bare period, which would hand that drift back.
        s.servo.integ = 20000.0
        s.api.read_memory = lambda a, n, timeout=1.0: None  # type: ignore[method-assign]
        held = servo_hold_period(20000.0, chunk_period=self.CHUNK_PERIOD)
        self.assertGreater(held, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD), held)
        self.assertEqual(s.servo.integ, 20000.0)

        # In-ring read: R=$4200, W=$4000+6000 → gap=(22384-16896)%8192=5488,
        # well above target, so the controller lengthens the period. Telemetry
        # records the gap.
        s.servo.integ = 0.0
        ahead_addr = RING_BUFFER_ADDR + 6000
        s.api.read_memory = lambda a, n, timeout=1.0: bytes([0x00, 0x42])  # type: ignore[method-assign]
        period = s.servo.next_pace_increment(ahead_addr, self.CHUNK_PERIOD)
        self.assertGreater(period, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.gap_last, (ahead_addr - 0x4200) % RING_BUFFER_SIZE)

    def test_the_integral_term_carries_from_one_chunk_to_the_next(self):
        # The integrator is the standing bus-halt correction: each chunk's
        # controller output has to start from the last one's, or the servo is
        # proportional-only and parks the gap off target by the steady drift.
        s = _new_streamer(use_reu_pump=False)
        s.host_dma_servo = True
        s.servo.integ = 0.0
        s.api.read_memory = lambda a, n, timeout=1.0: bytes([0x00, 0x42])  # type: ignore[method-assign]
        write_addr = RING_BUFFER_ADDR + 6000
        gap = (write_addr - 0x4200) % RING_BUFFER_SIZE
        _, after_one = servo_period(gap, 0.0, chunk_period=self.CHUNK_PERIOD)
        _, after_two = servo_period(gap, after_one, chunk_period=self.CHUNK_PERIOD)
        s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertNotEqual(after_one, 0.0)
        self.assertEqual(s.servo.integ, after_one)
        s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.integ, after_two)

    def test_ring_lead_smooths_the_gap(self):
        # One reading moves the lead a step toward the gap, never onto it, so
        # a torn R read cannot jump the A/V clock by a whole ring.
        s = _new_streamer(use_reu_pump=False)
        s.host_dma_servo = True
        s.servo.reset_for_consumer_start(4096)
        s.api.read_memory = lambda a, n, timeout=1.0: bytes([0x00, 0x40])  # type: ignore[method-assign]
        s.servo.next_pace_increment(RING_BUFFER_ADDR + 8000, self.CHUNK_PERIOD)
        self.assertAlmostEqual(s.servo.ring_lead, 4096 + RING_LEAD_EMA_ALPHA * (8000 - 4096))
        # R keeps moving, as a live consumer's does, at the same gap.
        for i in range(200):
            r_hi = 0x40 + (i % 2)
            s.api.read_memory = lambda a, n, timeout=1.0, _h=r_hi: bytes([0x00, _h])  # type: ignore[method-assign]
            w = RING_BUFFER_ADDR + 8000 + (r_hi - 0x40) * 0x100
            s.servo.next_pace_increment(w, self.CHUNK_PERIOD)
        self.assertAlmostEqual(s.servo.ring_lead, 8000, delta=1)

    def test_ring_lead_stays_unseeded_before_the_consumer_starts(self):
        # A worker that outlived stop()'s join can still read R once; that
        # reading must not turn the "no consumer" sentinel into a lead.
        s = _new_streamer(use_reu_pump=False)
        s.host_dma_servo = True
        s.servo.reset_after_stop()
        s.api.read_memory = lambda a, n, timeout=1.0: bytes([0x00, 0x40])  # type: ignore[method-assign]
        s.servo.next_pace_increment(RING_BUFFER_ADDR + 8000, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.ring_lead, -1.0)
        self.assertEqual(s.position_seconds(), 0.0)


class ReuPumpLatchDerivationTest(unittest.TestCase):
    """The pump latch is derived, never a constant: pump period = chunk x the
    NMI period, and the NMI period tracks [audio].sample_rate. One derivation
    serves both the video and mic bring-ups (_program_reu_pump_rate)."""

    def test_8khz_reproduces_the_historical_constant(self):
        s = _new_streamer(sample_rate=8000)
        self.assertEqual(s._program_reu_pump_rate(REU_PUMP_CHUNK_SIZE), REU_PUMP_CIA1_LATCH_8KHZ)

    def test_12khz_default_is_not_the_8khz_constant(self):
        s = _new_streamer()
        latch = s._program_reu_pump_rate(REU_PUMP_CHUNK_SIZE)
        self.assertEqual(latch, MATCHED_LATCH_12KHZ)
        self.assertLess(latch, REU_PUMP_CIA1_LATCH_8KHZ)

    def test_latch_is_recorded_and_written(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        latch = s._program_reu_pump_rate(80)
        self.assertEqual(latch, 80 * 85 - 1)
        self.assertEqual(s._reu_cia1_latch_nominal, latch)
        self.assertEqual(fake.memories["DC04"], _packed_latch(latch))

    def test_period_past_16_bits_is_clamped_with_a_warning(self):
        """A CIA latch is two 8-bit registers, so the write would silently
        reduce the period modulo 65536 — landing anywhere, including a latch
        that fires the pump hundreds of times faster than matched. Reachable
        from config: c64.nmi_rate_safety bounds only the fast end of
        sample_rate, so a low rate passes validation."""
        s = _new_streamer(sample_rate=1000)
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            latch = s._program_reu_pump_rate(REU_PUMP_CHUNK_SIZE)
        self.assertEqual(latch, CIA_TIMER_LATCH_MAX)
        self.assertTrue(any("16-bit maximum" in m for m in cm.output), cm.output)


class GovernedPumpOverdriveTest(unittest.TestCase):
    """A governor can only skip, so a governed pump has to out-produce the
    reader by itself; a matched one does not once bus halts cost it CIA #1
    ticks (#544: the tracked pump fell ~1.6 kB/s short under mhires bank-swap
    video). start_for_reu_staged therefore overdrives the pump rate exactly
    when a governor will trim it."""

    def _latch(self, *, governor: bool, skip_hook: bool) -> int:
        s = _new_streamer()
        s.reu_pump_governor = governor
        s.start_for_reu_staged(
            b"\x07" * RING_BUFFER_SIZE,
            chunk_size=64,
            skip_irq_vector_hook=skip_hook,
        )
        return s._reu_cia1_latch_nominal

    def test_governed_pumps_run_faster_than_matched(self):
        matched = 64 * 85 - 1
        for skip_hook in (False, True):
            with self.subTest(tracked=skip_hook):
                latch = self._latch(governor=True, skip_hook=skip_hook)
                self.assertEqual(latch, round(64 * 85 / REU_GOVERNOR_PUMP_OVERDRIVE) - 1)
                self.assertLess(latch, matched)

    def test_open_loop_pumps_stay_matched(self):
        # Without a governor nothing trims a surplus, so it would lap the ring.
        for skip_hook in (False, True):
            with self.subTest(tracked=skip_hook):
                self.assertEqual(self._latch(governor=False, skip_hook=skip_hook), 64 * 85 - 1)

    def test_overdrive_outruns_the_measured_tick_loss(self):
        # 10.0 kB/s read against 8.4 kB/s matched delivery (#544): the factor
        # must at least cover that ratio, or the reader still laps the writer.
        self.assertGreater(REU_GOVERNOR_PUMP_OVERDRIVE, 10.0 / 8.4)


class ReuAudioRegionBoundTest(unittest.TestCase):
    """One byte is one sample, so the staged upload's footprint grows with the
    track's duration and nothing about it is self-bounding."""

    def test_region_ends_at_or_below_the_video_staging_base(self):
        """The layout binding audio_handlers deliberately does not import (the
        audio layer keeps no dependency on the video layer). Asserted here so
        the two cannot drift apart silently."""
        from c64cast.video.modes_irq import REU_VIDEO_SCREEN_BASE

        self.assertLessEqual(REU_AUDIO_BASE + REU_AUDIO_MAX_BYTES, REU_VIDEO_SCREEN_BASE)

    def test_ordinary_track_is_uploaded_whole(self):
        s = _new_streamer()
        payload = b"\x07" * 4096
        self.assertIs(s._fit_reu_audio_region(payload, 1000), payload)

    def test_oversized_track_is_truncated_with_a_warning(self):
        """Past the ceiling the upload runs into the video staging region the
        REU bank-swap bitmap path rewrites every frame: the pump would DMA
        bitmap bytes into the ring as full-scale garbage while the video writes
        shred the audio, with no host-side error, on nothing more exotic than a
        long clip."""
        s = _new_streamer()
        pad = 1000
        payload = b"\x07" * (REU_AUDIO_MAX_BYTES + 1)
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            fitted = s._fit_reu_audio_region(payload, pad)
        self.assertEqual(len(fitted), REU_AUDIO_MAX_BYTES - pad)
        self.assertLessEqual(len(fitted) + pad, REU_AUDIO_MAX_BYTES)
        self.assertTrue(any("truncating" in m for m in cm.output), cm.output)


class ReuPreencodeDitherTest(unittest.TestCase):
    """The whole-track pre-encode is the third DAC encode site, and it draws
    its dither from the streamer's seed too — so the number a run logs
    re-encodes a REU-staged capture, not only a realtime one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = os.path.join(self.tmp.name, "clip.mp4")
        open(self.clip, "wb").close()
        # A tone rather than silence: the encoder skips dither on exact-zero
        # samples, so a silent track would compare equal under any seed.
        self.pcm = (np.sin(np.arange(4096) / 8.0) * 12000).astype(np.int16)

    def _staged(self, seed: int) -> bytes:
        s = new_streamer(dither=True, dither_seed=seed)
        scene = VideoScene(MagicMock(), s, MagicMock(), self.clip)
        with (
            mock.patch("c64cast.scenes.scenes.decode_audio_full", return_value=self.pcm),
            self.assertLogs("c64cast.scenes.scenes", level="INFO") as cm,
        ):
            encoded = scene._preencode_audio_for_reu()
        self.assertTrue(any("REU pre-encode" in m for m in cm.output), cm.output)
        return encoded

    def test_same_seed_reproduces_the_staged_encode(self):
        self.assertEqual(self._staged(4242), self._staged(4242))

    def test_different_seeds_differ(self):
        self.assertNotEqual(self._staged(4242), self._staged(9001))


class ReuPreencodeOriginTest(unittest.TestCase):
    """The pre-encode decodes on the picture's origin, pinned on the source
    before its demuxer starts, so a sound that starts after its picture
    keeps that distance in the REU (#606)."""

    def test_the_preload_decodes_on_the_pinned_origin(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clip = os.path.join(tmp.name, "clip.mp4")
        open(clip, "wb").close()
        pcm = (np.sin(np.arange(4096) / 8.0) * 12000).astype(np.int16)
        scene = VideoScene(MagicMock(), new_streamer(dither=False), MagicMock(), clip)
        scene.source = MagicMock()
        scene.source.pin_timeline_origin.return_value = 2.5
        with (
            mock.patch("c64cast.scenes.scenes.decode_audio_full", return_value=pcm) as decode,
            self.assertLogs("c64cast.scenes.scenes", level="INFO"),
        ):
            scene._preencode_audio_for_reu()
        scene.source.pin_timeline_origin.assert_called_once_with()
        self.assertEqual(
            decode.call_args.kwargs, {"origin_s": 2.5, "max_samples": REU_AUDIO_MAX_BYTES}
        )


class ReuPreencodeMarkerTest(unittest.TestCase):
    """``source_alignment_marker`` prepends a chirp to the staged bytes; the
    flag, the order, the length and the active DAC curve all had no test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = os.path.join(self.tmp.name, "clip.mp4")
        open(self.clip, "wb").close()
        self.pcm = (np.sin(np.arange(4096) / 8.0) * 12000).astype(np.int16)

    def _staged(self, *, marker: bool, curve=None) -> tuple[bytes, AudioStreamer]:
        s = new_streamer(dither=False)
        s._dac_curve = curve
        scene = VideoScene(MagicMock(), s, MagicMock(), self.clip, prepend_alignment_marker=marker)
        with (
            mock.patch("c64cast.scenes.scenes.decode_audio_full", return_value=self.pcm),
            self.assertLogs("c64cast.scenes.scenes", level="INFO"),
        ):
            return scene._preencode_audio_for_reu(), s

    def test_flag_off_adds_nothing(self):
        plain, _ = self._staged(marker=False)
        self.assertEqual(len(plain), len(self.pcm))

    def test_marker_leads_the_track_at_the_effective_rate(self):
        from c64cast.audio.audio_marker import marker_duration_samples, synthesize_marker

        plain, _ = self._staged(marker=False)
        marked, s = self._staged(marker=True)
        sr = int(round(s.effective_rate))
        n = marker_duration_samples(sr)
        self.assertEqual(len(marked), len(plain) + n)
        self.assertEqual(marked[:n], synthesize_marker(sr))
        self.assertEqual(marked[n:], plain)

    def test_marker_uses_the_active_dac_curve(self):
        curve = np.arange(256, dtype=np.uint8)[::-1].copy()
        marked, s = self._staged(marker=True, curve=curve)
        n = len(marked) - len(self.pcm)
        self.assertGreater(max(marked[:n]), 200)


class TrackedVideoPumpInstallFailureTest(unittest.TestCase):
    """The tracked video pump shares _install_tracked_pump with the mic pump
    (tested in test_reu_mic.TrackedPumpDeliveryTest). When a stage never
    confirms, start_for_reu_staged undoes its bring-up and raises, and
    VideoScene plays the run without audio instead of letting the exception
    reach the playlist."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = os.path.join(self.tmp.name, "clip.mp4")
        open(self.clip, "wb").close()

    @staticmethod
    def _lossy_streamer() -> AudioStreamer:
        s = _new_streamer()
        lose_writes_to(cast(FakeAPI, s.api), REU_AUDIO_SRC_TRACKER_ADDR)
        return s

    def test_start_raises_with_nothing_armed(self):
        s = self._lossy_streamer()
        fake = cast(FakeAPI, s.api)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertFalse(s._reu_pump_armed)
        self.assertFalse(s.running)
        self.assertFalse(any(o[:2] == ("write_memory_file", "C100") for o in fake.ops))
        self.assertEqual(fake.memories["C180"], "60")
        self.assertEqual(fake.regs["DD0D"][0], 0x7F)

    def test_video_scene_plays_the_run_without_audio(self):
        s = self._lossy_streamer()
        mode = MagicMock(
            audio_reu_pump_active=True, use_reu_staged=True, drives_rec_from_host=False
        )
        scene = VideoScene(MagicMock(), s, mode, self.clip, setup_progress=False)
        source = MagicMock(a_stream=object())
        with (
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource", return_value=source),
            mock.patch.object(VideoScene, "_preencode_audio_for_reu", return_value=b"\x07" * 4096),
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
        ):
            scene.setup()
        self.assertIsNone(scene.audio)
        source.start.assert_called_once_with(audio_push=None)
        # The transport clocks off the wall rather than a pump that never armed.
        scene.wall_start_time = 100.0
        with mock.patch("c64cast.scenes.video_transport.time.time", return_value=103.0):
            self.assertAlmostEqual(scene.transport.clock_s(), 3.0)
        # Teardown hands the streamer back for the next run.
        with quiet_logging():
            scene.teardown()
        self.assertIs(scene.audio, s)


class StagedPumpInstallDeliveryTest(unittest.TestCase):
    """Every write the staged pump's bring-up depends on is confirmed before
    $0314 is pointed at $C100: the plain handler, the REC registers and the
    CIA #1 latch, and the vector patch itself. A lost handler under a patched
    vector runs whatever $C100 held; lost REC registers leave the plain pump
    DMAing from wherever a bank-swap scene left $DF02-$DF06."""

    TRIES = audio_mod.TRACKED_PUMP_INSTALL_TRIES
    KERNAL_IRQ = (KERNAL.IRQ_HANDLER & 0xFF, KERNAL.IRQ_HANDLER >> 8)
    PUMP_IRQ = (REU_PUMP_HANDLER_ADDR & 0xFF, REU_PUMP_HANDLER_ADDR >> 8)

    def _start(self, lose: int, times: int | None, *, governor: bool = False, skip_hook=False):
        s = new_streamer(dither=False, use_reu_pump=True, reu_pump_governor=governor)
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, lose, times)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=skip_hook)
        return s, fake

    def _start_failing(self, lose: int, times: int | None, **kw):
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR") as cm,
            self.assertRaises(PumpInstallError),
        ):
            self._start(lose, times, **kw)
        self.assertTrue(any("plays without audio" in m for m in cm.output), cm.output)

    @staticmethod
    def _vector_writes(fake: FakeAPI) -> list[tuple]:
        return [o[2] for o in fake.ops if o[:2] == ("write_regs", "0314")]

    def test_each_install_write_is_flushed_before_the_vector_patch(self):
        for governor in (False, True):
            with self.subTest(governor=governor):
                _s, fake = self._start(0x0000, 0, governor=governor)
                ops = fake.ops
                handler = next(
                    i for i, o in enumerate(ops) if o[:2] == ("write_memory_file", "C100")
                )
                rec = next(i for i, o in enumerate(ops) if o[:2] == ("write_memory", "DF02"))
                latch = next(i for i, o in enumerate(ops) if o[:2] == ("write_memory", "DC04"))
                vector = next(i for i, o in enumerate(ops) if o[:2] == ("write_regs", "0314"))
                self.assertIn(("flush",), ops[handler:rec])
                self.assertIn(("flush",), ops[latch:vector])
                self.assertIn(("flush",), ops[vector:])

    def test_a_lost_handler_is_resent_and_the_pump_arms(self):
        s, fake = self._start(REU_PUMP_HANDLER_ADDR, 1)
        self.assertTrue(s._reu_pump_armed)
        self.assertEqual(self._vector_writes(fake), [self.PUMP_IRQ])

    def test_a_handler_that_never_lands_leaves_the_vector_alone(self):
        for governor in (False, True):
            with self.subTest(governor=governor):
                s = new_streamer(dither=False, use_reu_pump=True, reu_pump_governor=governor)
                fake = cast(FakeAPI, s.api)
                lose_writes_to(fake, REU_PUMP_HANDLER_ADDR)
                with (
                    self.assertLogs("c64cast.audio.audio", level="ERROR"),
                    self.assertRaises(PumpInstallError),
                ):
                    s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
                self.assertNotIn(self.PUMP_IRQ, self._vector_writes(fake))
                self.assertFalse(s._reu_pump_armed)
                self.assertFalse(s.running)
                self.assertEqual(fake.regs["DD0D"][0], 0x7F)
                self.assertEqual(fake.nmi_consumer_notes[-1], False)

    def test_lost_rec_or_latch_writes_abort_before_the_vector_patch(self):
        kernal_latch = _packed_latch(kernal_cia1_latch("NTSC"))
        for lost in (REU.C64_ADDR_LO, REU.LENGTH_LO, CIA1.TIMER_A_LO):
            with self.subTest(lost=f"${lost:04X}"):
                s = _new_streamer()
                fake = cast(FakeAPI, s.api)
                lose_writes_to(fake, lost, self.TRIES)
                with (
                    self.assertLogs("c64cast.audio.audio", level="ERROR"),
                    self.assertRaises(PumpInstallError),
                ):
                    s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
                self.assertNotIn(self.PUMP_IRQ, self._vector_writes(fake))
                self.assertFalse(s._reu_pump_armed)
                # The pump rate the install may have landed goes back to the kernal's.
                self.assertEqual(fake.memories["DC04"], kernal_latch)

    def test_a_vector_patch_that_never_confirms_is_restored_to_the_kernal(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(self._vector_writes(fake)[-1], self.KERNAL_IRQ)
        self.assertFalse(s._reu_pump_armed)
        self.assertEqual(fake.regs["DD0D"][0], 0x7F)

    def test_a_lost_vector_restore_is_resent(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES + 1)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(self._vector_writes(fake), [self.KERNAL_IRQ])
        # A restore that confirmed leaves nothing owed, so stop() has no
        # vector to write.
        s.stop()
        self.assertEqual(self._vector_writes(fake), [self.KERNAL_IRQ])

    def test_a_vector_restore_that_never_confirms_is_owed_to_stop(self):
        # Each patch lands but a redial moves the epoch behind it, so the pump
        # is live on $0314; then every restore is lost. Nothing armed, so only
        # the owed restore puts the kernal back when the scene stops.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        real_regs = fake.write_regs
        vector_writes = [0]

        def write_regs(base, *vals):
            if base.upper() == f"{VECTORS.IRQ:04X}" and vector_writes[0] < 2 * self.TRIES:
                vector_writes[0] += 1
                fake.delivery_epoch += 1
                if vector_writes[0] > self.TRIES:
                    return
            real_regs(base, *vals)

        fake.write_regs = write_regs  # type: ignore[method-assign]
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertEqual(fake.regs["0314"], self.PUMP_IRQ)
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)

    def _assert_entry_stubbed(self, fake: FakeAPI) -> None:
        # $0314 still names $C100, so every CIA #1 tick runs it: the stub
        # chains the kernal on each one, where the pump entry's divider did so
        # on every third (a slow jiffy clock, keyboard and cursor).
        self.assertEqual(fake.regs["0314"], self.PUMP_IRQ)
        self.assertEqual(fake.mem_files["C100"], REU_PUMP_HANDLER_STUB)
        self.assertEqual(fake.memories[f"{CIA1.ICR:04X}"], f"{CIA1.ICR_ENABLE_TIMER_A:02X}")
        # Under a mask: an IRQ may be fetching the entry the stub replaces.
        stub = max(
            i
            for i, o in enumerate(fake.ops)
            if o == ("write_memory_file", "C100", REU_PUMP_HANDLER_STUB)
        )
        icr = [o[2] for o in fake.ops[:stub] if o[:2] == ("write_memory", f"{CIA1.ICR:04X}")]
        self.assertEqual(icr[-1], f"{CIA1.ICR_DISABLE_ALL:02X}")

    def test_an_unwind_whose_vector_restore_never_lands_stubs_the_entry(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        real_regs = fake.write_regs
        vector_writes = [0]

        def write_regs(base, *vals):
            # Every patch lands behind a moved epoch; every restore is lost.
            if base.upper() == f"{VECTORS.IRQ:04X}":
                vector_writes[0] += 1
                fake.delivery_epoch += 1
                if vector_writes[0] > self.TRIES:
                    return
            real_regs(base, *vals)

        fake.write_regs = write_regs  # type: ignore[method-assign]
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertTrue(s._irq_vector_restore_owed)
        self._assert_entry_stubbed(fake)

    def test_a_stop_whose_vector_restore_never_lands_stubs_the_entry(self):
        s, fake = self._start(0x0000, 0)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()
        self.assertTrue(s._irq_vector_restore_owed)
        self._assert_entry_stubbed(fake)

    def test_an_unmask_lost_after_the_entry_stub_is_owed_to_stop(self):
        # The stub's mask lands and every unmask after it is lost, so CIA #1
        # is left masked: no kernal jiffy IRQ at all. The next stop() lands
        # the vector restore, which alone would not unmask, so the unmask is
        # owed beside it.
        s, fake = self._start(0x0000, 0)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        real_memory = fake.write_memory
        lost_unmasks = [2 * self.TRIES]

        def write_memory(addr, data_hex):
            if str(addr).upper() == icr and data_hex == unmask and lost_unmasks[0]:
                lost_unmasks[0] -= 1
                fake.delivery_epoch += 1
                return
            real_memory(addr, data_hex)

        fake.write_memory = write_memory  # type: ignore[method-assign]
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()
        self.assertEqual(fake.memories[icr], f"{CIA1.ICR_DISABLE_ALL:02X}")
        self.assertTrue(s._cia1_unmask_owed)
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)
        self.assertEqual(fake.memories[icr], unmask)
        self.assertFalse(s._cia1_unmask_owed)

    @staticmethod
    def _drop_unmasks(fake: FakeAPI) -> list[bool]:
        """Lose every CIA #1 unmask while the returned flag holds True."""
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        real_memory = fake.write_memory
        dropping = [True]

        def write_memory(addr, data_hex):
            if dropping[0] and str(addr).upper() == icr and data_hex == unmask:
                fake.delivery_epoch += 1
                return
            real_memory(addr, data_hex)

        fake.write_memory = write_memory  # type: ignore[method-assign]
        return dropping

    def test_an_owed_unmask_at_stop_waits_behind_a_vector_restore(self):
        # A dispatcher install whose entry and every unmask after it are lost
        # leaves CIA #1 masked with nothing armed. The dispatcher's uninstall
        # keeps its own mask when its $0314 restore is lost, so stop() unmasks
        # only once $0314 is back at the kernal.
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        s = new_streamer(dither=False, use_reu_pump=True)
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, REU_PUMP_HANDLER_ADDR, self.TRIES)
        dropping = self._drop_unmasks(fake)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertFalse(s._reu_pump_armed)
        self.assertTrue(s._cia1_unmask_owed)
        self.assertEqual(fake.memories[icr], f"{CIA1.ICR_DISABLE_ALL:02X}")
        fake.regs["0314"] = (0x00, 0xC5)
        dropping[0] = False
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)
        self.assertEqual(fake.memories[icr], unmask)
        self.assertFalse(s._cia1_unmask_owed)
        restore = max(
            i for i, o in enumerate(fake.ops) if o == ("write_regs", "0314", self.KERNAL_IRQ)
        )
        unmasks = [i for i, o in enumerate(fake.ops) if o == ("write_memory", icr, unmask)]
        self.assertLess(restore, unmasks[-1])

    def test_an_owed_unmask_stays_owed_when_the_stop_restore_is_lost(self):
        # The failed restore stubs $C100, but $0314 may still name the stale
        # dispatcher rather than $C100, so the stub's own unmask must not lift
        # a mask the restore did not place.
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        s = new_streamer(dither=False, use_reu_pump=True)
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, REU_PUMP_HANDLER_ADDR, self.TRIES)
        dropping = self._drop_unmasks(fake)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertTrue(s._cia1_unmask_owed)
        fake.regs["0314"] = (0x00, 0xC5)
        dropping[0] = False
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()
        self.assertEqual(fake.regs["0314"], (0x00, 0xC5))
        self.assertNotEqual(fake.memories[icr], unmask)
        self.assertTrue(s._cia1_unmask_owed)
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)
        self.assertEqual(fake.memories[icr], unmask)
        self.assertFalse(s._cia1_unmask_owed)

    def test_a_pump_armed_under_an_owed_unmask_unmasks_cia1(self):
        # Masked, CIA #1 raises no IRQ at all, so a pump armed on $0314
        # without the unmask never runs.
        s, fake = self._start(0x0000, 0)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        dropping = self._drop_unmasks(fake)
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()
        self.assertTrue(s._cia1_unmask_owed)
        dropping[0] = False
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertTrue(s._reu_pump_armed)
        self.assertEqual(fake.regs["0314"], self.PUMP_IRQ)
        self.assertEqual(fake.memories[f"{CIA1.ICR:04X}"], f"{CIA1.ICR_ENABLE_TIMER_A:02X}")
        self.assertFalse(s._cia1_unmask_owed)

    def test_an_arm_whose_owed_unmask_never_confirms_unwinds(self):
        # The pump would sit on $0314 under a mask that keeps it from running;
        # the unwind takes it off, and the unmask stays owed to stop().
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        s = new_streamer(dither=False, use_reu_pump=True)
        fake = cast(FakeAPI, s.api)
        s._cia1_unmask_owed = True
        dropping = self._drop_unmasks(fake)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE)
        self.assertFalse(s._reu_pump_armed)
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)
        self.assertTrue(s._cia1_unmask_owed)
        dropping[0] = False
        s.stop()
        self.assertEqual(fake.memories[icr], unmask)
        self.assertFalse(s._cia1_unmask_owed)

    def test_a_dispatcher_entry_upload_pays_an_owed_unmask(self):
        # The confirmed entry stage already unmasked CIA #1, so a second unmask
        # at arm is one more write a lossy link could fail the install on.
        icr, unmask = f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
        s = new_streamer(dither=False, use_reu_pump=True)
        fake = cast(FakeAPI, s.api)
        s._cia1_unmask_owed = True
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertTrue(s._reu_pump_armed)
        self.assertFalse(s._cia1_unmask_owed)
        self.assertEqual(fake.ops.count(("write_memory", icr, unmask)), 1)

    def test_a_lost_vector_restore_at_stop_is_resent(self):
        # stop()'s restore is the last write that can take an armed pump off
        # $0314; one lost on the link would leave it running past the scene.
        s, fake = self._start(0x0000, 0)
        self.assertTrue(s._reu_pump_armed)
        lose_writes_to(fake, VECTORS.IRQ, 1)
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)

    def test_a_stop_restore_that_never_confirms_stays_owed(self):
        # The streamer is shared across scenes: a restore stop() could not
        # land is written again by the next stop(), whatever that scene armed.
        s, fake = self._start(0x0000, 0)
        lose_writes_to(fake, VECTORS.IRQ, self.TRIES)
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()
        self.assertEqual(fake.regs["0314"], self.PUMP_IRQ)
        self.assertTrue(s._irq_vector_restore_owed)
        s.stop()
        self.assertEqual(fake.regs["0314"], self.KERNAL_IRQ)
        self.assertFalse(s._irq_vector_restore_owed)

    def test_a_tracked_install_whose_latch_never_lands_parks_the_body(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, CIA1.TIMER_A_LO, self.TRIES)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertEqual(fake.memories["C180"], "60")
        # The dispatcher owns $0314, so the unwind leaves it alone.
        self.assertEqual(self._vector_writes(fake), [])
        self.assertFalse(s._reu_pump_armed)

    def test_a_failed_dispatcher_arm_puts_the_entry_stub_back_before_the_latch(self):
        # The dispatcher keeps JMPing to $C100 after the abort. Left in place,
        # the tracked entry's tick divider would chain the kernal on every
        # third tick once CIA #1 is back at the kernal latch.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, CIA1.TIMER_A_LO, self.TRIES)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR"),
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.assertEqual(fake.mem_files["C100"], REU_PUMP_HANDLER_STUB)
        stub = max(
            i
            for i, o in enumerate(fake.ops)
            if o == ("write_memory_file", "C100", REU_PUMP_HANDLER_STUB)
        )
        icr = [o[2] for o in fake.ops[:stub] if o[:2] == ("write_memory", "DC0D")]
        self.assertEqual(icr[-1], "7F")
        latch = max(i for i, o in enumerate(fake.ops) if o[:2] == ("write_memory", "DC04"))
        self.assertLess(stub, latch)
        self.assertEqual(fake.memories["DC0D"], "81")


class TrackedVideoPumpEntryMaskTest(unittest.TestCase):
    """Under a bank-swap dispatcher the video pump's $C100 entry goes up the
    way the mic pump's does: CIA #1 masked first, unmasked after, since the
    dispatcher reaches $C100 on its own IRQs whatever $0314 holds."""

    def test_the_dispatcher_entry_upload_is_bracketed_by_a_cia1_mask(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, skip_irq_vector_hook=True)
        self.addCleanup(s.stop)
        self.assertTrue(s._reu_pump_armed)
        ops = fake.ops
        entry = next(
            i
            for i, o in enumerate(ops)
            if o[:2] == ("write_memory_file", "C100") and o[2] != REU_PUMP_HANDLER_STUB
        )
        icr = [(i, o[2]) for i, o in enumerate(ops) if o[:2] == ("write_memory", "DC0D")]
        self.assertEqual([v for i, v in icr if i < entry][-1:], ["7F"])
        self.assertEqual([v for i, v in icr if i > entry][:1], ["81"])


class GovernorChunkBoundTest(unittest.TestCase):
    def test_a_chunk_of_exactly_the_governor_maximum_is_accepted(self):
        from c64cast.audio.audio_handlers import REU_GOVERNOR_MAX_CHUNK

        # The refusal past it is test_start_refuses_a_governed_chunk_past_the_maximum.
        s = _new_streamer()
        s.reu_pump_governor = True
        # Empty audio returns right after the chunk checks, so only a
        # ValueError from those checks can reach the test.
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            s.start_for_reu_staged(b"", chunk_size=REU_GOVERNOR_MAX_CHUNK)
        self.assertTrue(any("empty data" in m for m in cm.output), cm.output)


class StagedUploadDeliveryTest(unittest.TestCase):
    """Each REUWRITE slice of the staged track and its EOF pad is confirmed
    delivered. Every track lands at REU_AUDIO_BASE, so a slice lost to a
    lossy redial would otherwise play the previous scene's audio there."""

    PAYLOAD = bytes(range(256)) * (3 * REU_UPLOAD_SLICE // 256)

    def _start(self, lose: int, times: int | None):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_reu_writes_to(fake, lose, times)
        return s, fake

    def test_every_slice_is_flushed_before_the_next(self):
        s, fake = self._start(-1, 0)
        s.start_for_reu_staged(self.PAYLOAD)
        writes = [i for i, o in enumerate(fake.ops) if o[0] == "reu_write"]
        self.assertGreater(len(writes), 3)
        for a, b in zip(writes, writes[1:], strict=False):
            self.assertIn(("flush",), fake.ops[a:b])

    def test_a_lost_slice_is_resent_and_the_pump_arms(self):
        s, fake = self._start(REU_AUDIO_BASE + REU_UPLOAD_SLICE, 1)
        s.start_for_reu_staged(self.PAYLOAD)
        self.assertTrue(s._reu_pump_armed)
        landed = dict(fake.socket_dma.reuwrites)
        self.assertEqual(
            landed[REU_AUDIO_BASE + REU_UPLOAD_SLICE],
            self.PAYLOAD[REU_UPLOAD_SLICE : 2 * REU_UPLOAD_SLICE],
        )

    def test_a_slice_that_never_lands_aborts_before_the_nmi_bring_up(self):
        eof_pad_start = REU_AUDIO_BASE + len(self.PAYLOAD)
        for lost in (REU_AUDIO_BASE + REU_UPLOAD_SLICE, eof_pad_start):
            with self.subTest(lost=f"${lost:06X}"):
                s, fake = self._start(lost, None)
                with (
                    self.assertLogs("c64cast.audio.audio", level="ERROR") as cm,
                    self.assertRaises(PumpInstallError),
                ):
                    s.start_for_reu_staged(self.PAYLOAD)
                self.assertTrue(any("plays without audio" in m for m in cm.output), cm.output)
                self.assertEqual(
                    fake.ops.count(("lost_reu", lost)), audio_mod.TRACKED_PUMP_INSTALL_TRIES
                )
                self.assertFalse(s._reu_pump_armed)
                self.assertFalse(s.running)
                self.assertNotIn(f"{NMI_ROUTINE_ADDR:04X}", fake.mem_files)
                self.assertNotIn("0314", fake.regs)

    def _refuse_reu_writes_to(self, fake: FakeAPI, reu_offset: int, times: int | None) -> None:
        """Each of the first ``times`` REU writes at ``reu_offset`` raises, as
        socket DMA's reuwrite does when a redial fails or is refused under
        backoff. Unlike a lossy redial it leaves ``delivery_epoch`` alone."""
        real = fake.reu_write
        remaining = [times]

        def reu_write(offset, data):
            if offset == reu_offset and remaining[0] != 0:
                if remaining[0] is not None:
                    remaining[0] -= 1
                raise SocketDMAError("socket dma: did not answer the last redial")
            real(offset, data)

        fake.reu_write = reu_write  # type: ignore[method-assign]

    def test_a_slice_whose_write_raises_is_resent(self):
        s, fake = self._start(-1, 0)
        lost = REU_AUDIO_BASE + REU_UPLOAD_SLICE
        self._refuse_reu_writes_to(fake, lost, 1)
        s.start_for_reu_staged(self.PAYLOAD)
        self.assertTrue(s._reu_pump_armed)
        self.assertEqual(
            dict(fake.socket_dma.reuwrites)[lost],
            self.PAYLOAD[REU_UPLOAD_SLICE : 2 * REU_UPLOAD_SLICE],
        )

    def test_a_slice_whose_write_always_raises_aborts_like_a_lost_one(self):
        s, fake = self._start(-1, 0)
        lost = REU_AUDIO_BASE + REU_UPLOAD_SLICE
        self._refuse_reu_writes_to(fake, lost, None)
        with (
            self.assertLogs("c64cast.audio.audio", level="ERROR") as cm,
            self.assertRaises(PumpInstallError),
        ):
            s.start_for_reu_staged(self.PAYLOAD)
        self.assertTrue(any("plays without audio" in m for m in cm.output), cm.output)
        self.assertNotIn(lost, dict(fake.socket_dma.reuwrites))
        self.assertFalse(s._reu_pump_armed)
        self.assertNotIn(f"{NMI_ROUTINE_ADDR:04X}", fake.mem_files)


if __name__ == "__main__":
    unittest.main()
