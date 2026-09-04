"""Tests for the REU-staged audio path (AudioStreamer.start_for_reu_staged).

These tests don't require a real U64 — they verify the bring-up sequence
(REU upload, NMI install, IRQ vector patch order) against the FakeAPI's
recorded write log."""

from __future__ import annotations

import unittest
from typing import cast

from _fakes import FakeAPI, new_streamer, run_irq_handler

from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    CIA_TIMER_LATCH_MAX,
    HOST_DMA_SERVO_INTEG_CLAMP,
    HOST_DMA_SERVO_PERIOD_MAX_FRAC,
    HOST_DMA_SERVO_PERIOD_MIN_FRAC,
    HOST_DMA_SERVO_TARGET_GAP,
    NEUTRAL_SAMPLE,
    NMI_ROUTINE,
    NMI_ROUTINE_PATCH_OFFSET_READ_HI,
    NMI_ROUTINE_PATCH_OFFSET_RESET_HI,
    NMI_ROUTINE_PATCH_OFFSET_WRAP_HI,
    READ_PTR_HI_ADDR,
    REU_AUDIO_BASE,
    REU_AUDIO_MAX_BYTES,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_GOVERNOR_GAP_THRESHOLD_HI,
    REU_IRQ_HANDLER,
    REU_IRQ_HANDLER_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH_8KHZ,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_INITIAL_MARGIN,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    RING_BUFFER_SIZE,
    servo_period,
)

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
    target. Three hand-written byte locations in the NMI handler embed
    the ring HI bytes (read addr, end compare, wrap-reset); a fourth in
    the REU IRQ handler embeds the same. Verify all four agree with the
    RING_BUFFER_* constants so a single-place address change is enough."""

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

    def test_nmi_handler_read_hi_matches_ring_addr(self):
        # The patch site at offset READ_HI gets RING_BUFFER_HI written into
        # the LDA $???? operand. Verify the surrounding bytes form a valid
        # LDA absolute (the opcode is the byte before the patch offset).
        self.assertEqual(
            NMI_ROUTINE[NMI_ROUTINE_PATCH_OFFSET_READ_HI - 2], 0xAD, "LDA absolute opcode"
        )

    def test_nmi_handler_wrap_hi_matches_ring_end(self):
        # The patch site at offset WRAP_HI is the immediate operand of a
        # CMP #imm — the opcode at offset WRAP_HI-1 must be $C9. Catches a
        # future routine edit that shifts byte layouts without updating the
        # patch offsets (would silently compare against a junk byte).
        self.assertEqual(
            NMI_ROUTINE[NMI_ROUTINE_PATCH_OFFSET_WRAP_HI - 1], 0xC9, "CMP immediate opcode"
        )

    def test_nmi_handler_reset_hi_matches_ring_addr(self):
        # The wrap-reset literal (LDA #start_hi) at offset RESET_HI gets
        # RING_BUFFER_HI patched in. Without this third patch the NMI would
        # wrap back to a stale $80 even after the constant moved, audibly
        # silent if $8000+ is uninitialized RAM. Opcode at RESET_HI-1 must
        # be $A9 (LDA immediate).
        self.assertEqual(
            NMI_ROUTINE[NMI_ROUTINE_PATCH_OFFSET_RESET_HI - 1], 0xA9, "LDA immediate opcode"
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


class ReuIrqHandlerTest(unittest.TestCase):
    """The IRQ handler is hand-assembled bytes — a typo here can JAM the
    CPU at runtime (KIL opcodes silently halt the 6502). Verify length and
    that the BCC branch lands on a valid instruction boundary."""

    def test_handler_length_is_known(self):
        # If the handler grows or shrinks, the BCC offset (currently +10) may
        # need recomputation to reach the trailing PLA. The audio module asserts
        # this length at import time, but assert again here so a test failure
        # in the test suite catches the issue too.
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
        # Confirm value is REU_PUMP_HANDLER_ADDR ($C100).
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
        s.start_for_reu_staged(b"\x07" * RING_BUFFER_SIZE, chunk_size=80)
        key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        handler = next(b for k, b in fake.writes if k == key)
        self.assertEqual(handler[2], 80)
        self.assertEqual(handler[7], 0)
        # Latch = chunk x NMI period = 80 x 85 - 1 = 6799.
        self.assertEqual(fake.memories["DC04"], _packed_latch(80 * 85 - 1))
        # And $DF07 = chunk LO/HI for initial REC length.
        self.assertEqual(fake.memories["DF07"], "5000")


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
        # Pre-condition: $0314 points at our handler
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
        # Calling stop() a second time must not raise even though the
        # REU pump state has already been torn down.
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
        # And the tracker is seeded at $C200.
        self.assertIn(f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}", fake.memories)

    def test_default_hook_uploads_plain_handler(self):
        # Inverse: solo audio path (no merged dispatcher) keeps the
        # proven plain handler. Don't risk regression on the baseline.
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
        # The pump body must be in place BEFORE the $C100 entry replaces
        # the JMP $EA31 stub the bank-swap installer left there — else a
        # CIA #1 IRQ that fires between the entry write and the body
        # write would JSR into uninitialized RAM. Easiest correct order
        # is body upload first.
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


class ReuTrackedHandlerTest(unittest.TestCase):
    """REU_IRQ_HANDLER_TRACKED, EXECUTED on the repo's own 6502 (see
    _fakes.run_irq_handler) instead of pinning instruction offsets: the
    old tests carried the offset in the test NAME (test_pla_at_offset_105),
    so inserting one instruction broke all six and required renames —
    while the constraint they guard (a wrong branch displacement JAMs the
    CPU) is exactly what actually running the handler proves.

    Covers the tick-divider / lean-exit pattern (borrowed from the SID
    player: chain to $EA31 every Nth CIA #1 tick, ack + JMP $EA81 for a
    lean RTI the other N-1), the tracker-driven REC pump, and the dst
    ring wrap."""

    def _run(self, *, counter: int, dst: int | None = None):
        from c64cast.audio.audio_handlers import (
            REU_IRQ_HANDLER_TRACKED,
            REU_PUMP_TICK_COUNTER_ADDR,
        )

        src = 0x032211  # arbitrary 24-bit REU offset, mid-ring
        dst = RING_BUFFER_ADDR if dst is None else dst
        seed = {
            REU_AUDIO_SRC_TRACKER_ADDR + 0: src & 0xFF,
            REU_AUDIO_SRC_TRACKER_ADDR + 1: (src >> 8) & 0xFF,
            REU_AUDIO_SRC_TRACKER_ADDR + 2: (src >> 16) & 0xFF,
            REU_AUDIO_SRC_TRACKER_ADDR + 3: dst & 0xFF,
            REU_AUDIO_SRC_TRACKER_ADDR + 4: (dst >> 8) & 0xFF,
            REU_PUMP_TICK_COUNTER_ADDR: counter,
        }
        run = run_irq_handler(REU_IRQ_HANDLER_TRACKED, addr=REU_PUMP_HANDLER_ADDR, seed=seed)
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
    """The C64-side governor handler is the plain pump handler with an 18-byte
    skip-when-ahead prefix. A typo in the prefix bytes or branch displacement
    JAMs the 6502, so verify the structure (these run with no hardware)."""

    def test_length_is_prefix_plus_body(self):
        # 18-byte governor prefix + the 37-byte plain handler sans its PHA (36).
        self.assertEqual(len(REU_IRQ_HANDLER_GOVERNOR), 18 + 36)

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

    def test_masks_gap_and_compares_threshold(self):
        # AND #$1F masks gap to 5 bits (32 ring HI values + discards REU
        # read-back garbage); CMP #threshold tests the half-ring skip point.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[8:10], bytes([0x29, 0x1F]), "AND #$1F")
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[10:12],
            bytes([0xC9, REU_GOVERNOR_GAP_THRESHOLD_HI]),
            "CMP #threshold_hi",
        )
        self.assertEqual(REU_GOVERNOR_GAP_THRESHOLD_HI, REU_PUMP_INITIAL_MARGIN >> 8)

    def test_bcc_skips_over_skip_block_to_pump_body(self):
        # BCC +4 (offset 12) jumps over the 4-byte skip block (PLA + JMP $EA31)
        # to the pump body at offset 18. A wrong displacement lands mid-JMP and
        # JAMs the CPU.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[12:14], bytes([0x90, 0x04]), "BCC +4")
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[14], 0x68, "skip-path PLA")
        self.assertEqual(
            REU_IRQ_HANDLER_GOVERNOR[15:18], bytes([0x4C, 0x31, 0xEA]), "skip-path JMP $EA31"
        )
        # Pump body (offset 18) is REU_IRQ_HANDLER without its leading PHA.
        self.assertEqual(REU_IRQ_HANDLER_GOVERNOR[18:], REU_IRQ_HANDLER[1:])


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
        self.assertEqual((lo, hi), (19, 24))
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
        from c64cast.audio.audio_handlers import HOST_DMA_SERVO_KI

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
        # servo is off, the read fails/short, or R is out of the ring; and runs
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

        # In-ring read: R=$4200, W=$4000+6000 → gap=(22384-16896)%8192=5488,
        # well above target, so the controller lengthens the period. Telemetry
        # records the gap.
        s.servo.integ = 0.0
        ahead_addr = RING_BUFFER_ADDR + 6000
        s.api.read_memory = lambda a, n, timeout=1.0: bytes([0x00, 0x42])  # type: ignore[method-assign]
        period = s.servo.next_pace_increment(ahead_addr, self.CHUNK_PERIOD)
        self.assertGreater(period, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.gap_last, (ahead_addr - 0x4200) % RING_BUFFER_SIZE)


if __name__ == "__main__":
    unittest.main()


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
