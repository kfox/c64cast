"""Where the REU bank-swap dispatchers commit a copied frame.

The commit is a $DD00 flip, plus bg0 and color RAM on mhires. It has to land
after the last bitmap line of one field and before the first badline of the
next, and it runs under py65 here so the window's edges are the real bytes'.
"""

from __future__ import annotations

import unittest
from functools import cache, partial
from typing import cast

import numpy as np
from _fakes import FakeAPI

from c64cast.audio.audio_handlers import (
    CHUNK_SIZE,
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    READ_PTR_HI_ADDR,
    READ_PTR_LO_ADDR,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
)
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    CIA_TIMER_LATCH_MAX,
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    KERNAL,
    NMI_CEILING_LATCH,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    RASTER_COMMIT_LAST_SAFE_LINE,
    RASTER_VBLANK_LINE,
    REU,
    SCREEN,
    RegionID,
    actual_rate_for_latch,
)
from c64cast.video import modes_irq
from c64cast.video.modes.hires import HiresDisplayMode
from c64cast.video.modes_irq import (
    BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER_ADDR,
    BORDER_SHOWN_ADDR,
    BORDER_STALE,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FRAME_TRACKER_ADDR,
    HOSTDMA_SWAP_IRQ_HANDLER,
    MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_BANK_SWAP_IRQ_HANDLER,
    MHIRES_FRAME_TRACKER_LEN,
    MHIRES_TRACKER_OFF_COLOR_REGS,
    REU_VIDEO_BITMAP_COLOR_LEN,
    TRACKER_OFF_BORDER,
)
from c64cast.video.palette import C64_PALETTE_BGR

# The VIC's picture at the default YSCROLL with 25 rows: the first badline,
# and the last pixel line of cell row 24.
FIRST_BADLINE = 51
LAST_PICTURE_LINE = FIRST_BADLINE + 25 * 8 - 1  # 250

DISPATCHERS = (
    ("hires", BANK_SWAP_IRQ_HANDLER, False),
    ("mhires", MHIRES_BANK_SWAP_IRQ_HANDLER, True),
    ("hires+pump", BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER, False),
    ("mhires+pump", MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER, True),
)


def prime_reu(mem, *, mhires: bool) -> None:
    """A REU dispatcher with a copied frame waiting for its commit."""
    for i, b in enumerate(modes_irq.BANK_SWAP_STATE_INIT):
        mem[modes_irq.BANK_SWAP_STATE_ADDR + i] = b
    mem[modes_irq._COPIED] = 1
    if mhires:
        color = modes_irq._SNAPSHOT + MHIRES_TRACKER_OFF_COLOR_REGS
        dest, length = SCREEN.COLOR_RAM, REU_VIDEO_BITMAP_COLOR_LEN
        regs = [dest & 0xFF, dest >> 8, 0x00, 0x30, 0xE1, length & 0xFF, length >> 8]
        for i, b in enumerate(regs):
            mem[color + i] = b


def prime_page_flip(mem, *, flicker: bool) -> None:
    """A host-DMA or flicker page flip with a staged frame; the flicker phase
    is set so this field's toggle lands on phase 0, the one it commits on."""
    tracker = [0x05, DD00_BANK_2, 0x01]
    if flicker:
        tracker += [0x01, D018_HIRES_PAGE_A, D018_HIRES_PAGE_B]
    for i, b in enumerate(tracker):
        mem[FRAME_TRACKER_ADDR + i] = b


def run_commit(handler: bytes, *, prime, line: int) -> list[tuple[str, int]]:
    """One raster IRQ at `line` through `handler`, its state set by `prime`.

    Returns each $DD00 / $D020 / $D021 write and each REU chunk's end, stamped
    in cycles from the end of the dispatcher's `LDA $D012`. A chunk halts the
    CPU for its length, which py65 does not model, so the stamps add it."""
    from py65.devices.mpu6502 import MPU
    from py65.memory import ObservableMemory

    mem = ObservableMemory()
    for i, b in enumerate(handler):
        mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
    prime(mem)
    mem[REU_PUMP_BODY_SUBROUTINE_ADDR] = 0x60  # RTS
    mem[0xD019] = 0x01
    mem[0xD012] = line
    mem[CIA1.ICR] = 0x00

    pending: list[str] = []
    for name, address in (("dd00", CIA2.PORT_A), ("d020", 0xD020), ("d021", 0xD021)):
        mem.subscribe_to_write([address], lambda _a, _v, name=name: pending.append(name))
    mem.subscribe_to_write([REU.COMMAND], lambda _a, _v: pending.append("chunk"))

    mpu = MPU(memory=mem)
    mpu.pc = BANK_SWAP_IRQ_HANDLER_ADDR
    halted = 0
    read_at: int | None = None
    events: list[tuple[str, int]] = []
    for _ in range(20000):
        if mpu.pc == KERNAL.IRQ_HANDLER:
            return events
        reads_raster = mem[mpu.pc] == 0xAD and (mem[mpu.pc + 1], mem[mpu.pc + 2]) == (0x12, 0xD0)
        mpu.step()
        if reads_raster and read_at is None:
            read_at = mpu.processorCycles
        for name in pending:
            if name == "chunk":
                halted += modes_irq.BANK_SWAP_CHUNK_SIZE
            assert read_at is not None, f"{name} written before the raster was read"
            events.append((name, mpu.processorCycles + halted - read_at))
        pending.clear()
    raise AssertionError("dispatcher never chained to the kernal")


def commits(handler: bytes, *, mhires: bool, line: int) -> bool:
    prime = partial(prime_reu, mhires=mhires)
    return any(name == "dd00" for name, _ in run_commit(handler, prime=prime, line=line))


class WindowStartTest(unittest.TestCase):
    """The window opens below the picture (#667). At line 248, past the last
    badline but not the last bitmap line, a commit put the next frame's bitmap
    in the bottom two pixel lines for a field."""

    def test_the_irq_line_is_the_first_line_below_the_picture(self):
        self.assertEqual(RASTER_VBLANK_LINE, LAST_PICTURE_LINE + 1)

    def test_no_dispatcher_commits_on_the_bottom_rows_last_lines(self):
        for name, handler, mhires in DISPATCHERS:
            for line in (243, 248, 249, LAST_PICTURE_LINE):
                with self.subTest(mode=name, line=line):
                    self.assertFalse(commits(handler, mhires=mhires, line=line))

    def test_every_dispatcher_commits_on_the_first_line_below_the_picture(self):
        for name, handler, mhires in DISPATCHERS:
            with self.subTest(mode=name):
                self.assertTrue(commits(handler, mhires=mhires, line=LAST_PICTURE_LINE + 1))


# --- Border (#668) -----------------------------------------------------------

HIRES_DISPATCHERS = tuple(d for d in DISPATCHERS if not d[2])
MHIRES_DISPATCHERS = tuple(d for d in DISPATCHERS if d[2])


def prime_border(mem, *, border: int, shown: int) -> None:
    """A hires commit waiting with `border` in its snapshot, after a commit
    that wrote `shown` (or a host stale mark)."""
    prime_reu(mem, mhires=False)
    mem[modes_irq._SNAPSHOT + TRACKER_OFF_BORDER] = border
    mem[BORDER_SHOWN_ADDR] = shown


def run_border_commit(handler: bytes, *, border: int, shown: int, line: int):
    """The commit's events and the border state it leaves: ($D020, memo)."""
    seen = {}

    def prime(mem):
        prime_border(mem, border=border, shown=shown)
        mem[0xD020] = 0xFF
        seen["mem"] = mem

    events = run_commit(handler, prime=prime, line=line)
    mem = seen["mem"]
    return [n for n, _ in events], mem[0xD020], mem[BORDER_SHOWN_ADDR]


class BorderCommitTest(unittest.TestCase):
    """The hires commit writes the frame's border as it flips to the frame,
    so the border cannot change ahead of the picture (#668)."""

    def test_a_stale_border_is_written_after_the_flip(self):
        for name, handler, _ in HIRES_DISPATCHERS:
            with self.subTest(mode=name):
                names, d020, shown = run_border_commit(
                    handler, border=0x06, shown=BORDER_STALE | 0x06, line=0
                )
                self.assertEqual([n for n in names if n != "chunk"], ["dd00", "d020"])
                self.assertEqual((d020, shown), (0x06, 0x06))

    def test_a_changed_border_is_written(self):
        for name, handler, _ in HIRES_DISPATCHERS:
            with self.subTest(mode=name):
                _, d020, shown = run_border_commit(handler, border=0x02, shown=0x06, line=0)
                self.assertEqual((d020, shown), (0x02, 0x02))

    def test_a_border_already_shown_is_left_alone(self):
        # The host pokes $D020 red while a loop is armed; a commit that
        # rewrote an unchanged border would erase it at the next frame.
        for name, handler, _ in HIRES_DISPATCHERS:
            with self.subTest(mode=name):
                names, d020, _ = run_border_commit(handler, border=0x06, shown=0x06, line=0)
                self.assertNotIn("d020", names)
                self.assertEqual(d020, 0xFF)

    def test_no_border_is_written_outside_the_window(self):
        for name, handler, _ in HIRES_DISPATCHERS:
            with self.subTest(mode=name):
                names, d020, _ = run_border_commit(
                    handler, border=0x06, shown=BORDER_STALE, line=LAST_PICTURE_LINE
                )
                self.assertEqual(names, [])
                self.assertEqual(d020, 0xFF)

    def test_the_state_starts_with_the_border_stale(self):
        self.assertEqual(
            modes_irq.BANK_SWAP_STATE_INIT[BORDER_SHOWN_ADDR - modes_irq.BANK_SWAP_STATE_ADDR],
            BORDER_STALE,
        )

    def test_mhires_leaves_the_border_to_the_host(self):
        for name, handler, mhires in MHIRES_DISPATCHERS:
            with self.subTest(mode=name):
                events = run_commit(handler, prime=partial(prime_reu, mhires=mhires), line=0)
                self.assertNotIn("d020", [n for n, _ in events])


class HiresBorderPushTest(unittest.TestCase):
    """What the host sends for the border on each hires path."""

    def _push(self, mode, color_index):
        fake = FakeAPI()
        frame = np.zeros((200, 320, 3), dtype=np.uint8)
        frame[:] = C64_PALETTE_BGR[color_index]
        mode.render(cast(Ultimate64API, fake), frame)
        return fake

    def test_reu_staging_carries_the_border_in_the_tracker(self):
        fake = self._push(HiresDisplayMode(use_reu_staged=True), 6)
        tracker = fake.mem_files[f"{FRAME_TRACKER_ADDR:04X}"]
        border = tracker[TRACKER_OFF_BORDER]
        self.assertNotIn(0xD020, fake.regions, "the border must not be written ahead of its frame")
        self.assertEqual(fake.regions[BORDER_SHOWN_ADDR], bytes([BORDER_STALE | border]))
        stale_at = next(
            i for i, op in enumerate(fake.ops) if op[:2] == ("write_region", BORDER_SHOWN_ADDR)
        )
        tracker_at = next(
            i
            for i, op in enumerate(fake.ops)
            if op[:2] == ("write_memory_file", f"{FRAME_TRACKER_ADDR:04X}")
        )
        self.assertLess(stale_at, tracker_at)
        self.assertEqual(fake.ops[stale_at][3], RegionID.VIC_D020)

    def test_a_host_dma_flip_writes_the_border_just_before_arming(self):
        fake = self._push(HiresDisplayMode(double_buffer=True), 6)
        kinds = [(op[0], op[1]) for op in fake.ops if op[0] != "write_memory"]
        border_at = kinds.index(("write_region", 0xD020))
        arm_at = kinds.index(("write_memory_file", f"{FRAME_TRACKER_ADDR:04X}"))
        self.assertEqual(border_at, arm_at - 1)


# --- Commit budget (#669) ---------------------------------------------------

CYCLES_PER_LINE_PAL = 63  # the tighter system: NTSC lines are 64-65 cycles


def nmi_cycles(read_ptr: int) -> int:
    """One audio NMI with the streamer's read pointer at `read_ptr`: the
    7-cycle interrupt sequence plus NMI_ROUTINE run to its RTI."""
    from py65.devices.mpu6502 import MPU
    from py65.memory import ObservableMemory

    mem = ObservableMemory()
    for i, b in enumerate(NMI_ROUTINE):
        mem[NMI_ROUTINE_ADDR + i] = b
    mem[READ_PTR_LO_ADDR], mem[READ_PTR_HI_ADDR] = read_ptr & 0xFF, read_ptr >> 8
    mpu = MPU(memory=mem)
    mpu.pc = NMI_ROUTINE_ADDR
    for _ in range(64):
        rti = mem[mpu.pc] == 0x40
        mpu.step()
        if rti:
            return mpu.processorCycles + 7
    raise AssertionError("NMI routine never returned")


def ring_write_size(profile: object, system: str, latch: int) -> int:
    """The audio-ring write the DAC streamer sends at `latch` on `system`
    over a link with `profile`, in bytes: its own sizing code, run on a
    stand-in for the streamer."""
    from types import SimpleNamespace

    from c64cast.audio.audio import AudioStreamer

    streamer = SimpleNamespace(
        nmi=SimpleNamespace(latch=latch),
        api=SimpleNamespace(profile=profile),
        chunk_size=CHUNK_SIZE,
        effective_rate=actual_rate_for_latch(latch, system),
    )
    return AudioStreamer._halt_quantum(streamer)  # type: ignore[arg-type]


@cache
def largest_ring_write() -> int:
    """The longest audio-ring write the DAC streamer sends, in bytes, at any
    rate it arms, on either backend and either system.

    The streamer cuts its writes to fit an NMI period, then raises them to
    what the link's write rate can carry. Its own sizing code is run at every
    latch from the fastest to the slowest the CIA timer holds."""
    from c64cast.hw.backend import BASE_PROFILES

    return max(
        ring_write_size(profile, system, latch)
        for profile in BASE_PROFILES.values()
        for system in ("PAL", "NTSC")
        for latch in range(NMI_CEILING_LATCH, CIA_TIMER_LATCH_MAX + 1)
    )


def worst_case_cycles(own: int) -> int:
    """`own` cycles of handler work and REU halt, stretched by everything that
    can take the bus while it runs.

    Audio NMIs at the fastest rate the streamer arms, each on the routine's
    short path but one on its longest (the ring wrap, once per 8 KB), and one
    host DMA halt as long as the longest the host sends while a REU-staged
    scene plays: an audio-ring write, since frames go to the REU without a
    halt and the frame tracker is shorter. Host writes are milliseconds apart
    on either link, far longer than this window, so one is all that can land
    in it. The host-DMA page flips' own frame writes are left out: they are
    up to 8000 bytes, and one that starts between the gate and the flip is
    the residual that only REU staging removes. Sprites are left out too: no
    bitmap scene enables them."""
    period = NMI_SAFE_MIN_PERIOD_CYCLES
    fast = nmi_cycles(RING_BUFFER_ADDR + 0x10)
    slow = max(nmi_cycles(RING_BUFFER_ADDR + 0xFF), nmi_cycles(RING_BUFFER_END - 1))
    halt = max(largest_ring_write(), MHIRES_FRAME_TRACKER_LEN)
    total = own + halt
    while True:
        nmis = total // period + 1
        grown = own + halt + nmis * fast + (slow - fast)
        if grown == total:
            return total
        total = grown


def latest_safe_read_line(cycles: int) -> int:
    """The last line on which a read whose consequence lands `cycles` later
    still lands before the first badline, read at the line's last cycle."""
    line = FIRST_BADLINE - 1
    while (FIRST_BADLINE - line) * CYCLES_PER_LINE_PAL - (CYCLES_PER_LINE_PAL - 1) < cycles:
        line -= 1
    return line


# Each handler with the state its commit needs, and the write that has to
# land before the first badline: the flip, on REU hires the border written
# after it (the picture's first line has a side border too), or on mhires the
# first color-RAM chunk, which holds cell row 0's colors.
HANDLERS = (
    ("hires", BANK_SWAP_IRQ_HANDLER, partial(prime_reu, mhires=False), "d020"),
    (
        "hires+pump",
        BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
        partial(prime_reu, mhires=False),
        "d020",
    ),
    ("mhires", MHIRES_BANK_SWAP_IRQ_HANDLER, partial(prime_reu, mhires=True), "chunk"),
    (
        "mhires+pump",
        MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
        partial(prime_reu, mhires=True),
        "chunk",
    ),
    ("hostdma", HOSTDMA_SWAP_IRQ_HANDLER, partial(prime_page_flip, flicker=False), "dd00"),
    ("flicker", FLICKER_SWAP_IRQ_HANDLER, partial(prime_page_flip, flicker=True), "dd00"),
)


def last_commit_line(handler: bytes, prime) -> int:
    """The window's far edge, found by running the handler on each line."""
    lines = [
        line
        for line in range(FIRST_BADLINE)
        if any(name == "dd00" for name, _ in run_commit(handler, prime=prime, line=line))
    ]
    assert lines == list(range(len(lines))), f"the window has a hole: {lines}"
    return lines[-1]


class RingWriteCapTest(unittest.TestCase):
    """RING_WRITE_HALT_CAP_BYTES is the write the fastest rate already gets
    on each backend, so a slower rate's write never outgrows it."""

    def test_the_cap_is_the_fastest_rates_write(self):
        from c64cast.audio.audio import RING_WRITE_HALT_CAP_BYTES
        from c64cast.hw.backend import BASE_PROFILES

        for name, profile in BASE_PROFILES.items():
            for system in ("PAL", "NTSC"):
                with self.subTest(backend=name, system=system):
                    self.assertEqual(
                        ring_write_size(profile, system, NMI_CEILING_LATCH),
                        RING_WRITE_HALT_CAP_BYTES,
                    )


class CommitBudgetTest(unittest.TestCase):
    """The commit started on the window's last line still lands before the
    first badline, under the worst bus load the streamer allows (#669)."""

    def test_every_handler_lands_before_the_first_badline(self):
        for name, handler, prime, deadline_write in HANDLERS:
            with self.subTest(handler=name):
                last = last_commit_line(handler, prime)
                events = run_commit(handler, prime=prime, line=last)
                own = next(t for n, t in events if n == deadline_write)
                worst = worst_case_cycles(own)
                self.assertLessEqual(
                    last,
                    latest_safe_read_line(worst),
                    f"{deadline_write} lands {worst} cycles after the read on line {last}",
                )

    def test_the_full_window_was_too_long_for_mhires(self):
        """Pins the measurement behind MHIRES_COMMIT_LAST_SAFE_LINE: the
        shared window's end, which the flip-only handlers keep, cannot carry
        the color copy."""
        events = run_commit(
            MHIRES_BANK_SWAP_IRQ_HANDLER, prime=partial(prime_reu, mhires=True), line=0
        )
        own = next(t for n, t in events if n == "chunk")
        self.assertLess(latest_safe_read_line(worst_case_cycles(own)), RASTER_COMMIT_LAST_SAFE_LINE)

    def test_the_full_window_was_too_long_for_the_hires_border(self):
        """Pins the measurement behind HIRES_COMMIT_LAST_SAFE_LINE: the flip
        fits the shared window, and the border written after it does not."""
        for name, handler, _ in HIRES_DISPATCHERS:
            with self.subTest(mode=name):
                events = run_commit(handler, prime=partial(prime_reu, mhires=False), line=0)
                flip, border = (next(t for n, t in events if n == w) for w in ("dd00", "d020"))
                self.assertGreaterEqual(
                    latest_safe_read_line(worst_case_cycles(flip)), RASTER_COMMIT_LAST_SAFE_LINE
                )
                self.assertLess(
                    latest_safe_read_line(worst_case_cycles(border)), RASTER_COMMIT_LAST_SAFE_LINE
                )


if __name__ == "__main__":
    unittest.main()
