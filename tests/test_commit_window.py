"""Where the REU bank-swap dispatchers commit a copied frame.

The commit is a $DD00 flip, plus bg0 and color RAM on mhires. It has to land
after the last bitmap line of one field and before the first badline of the
next, and it runs under py65 here so the window's edges are the real bytes'.
"""

from __future__ import annotations

import unittest
from functools import partial

from c64cast.audio.audio_handlers import (
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    READ_PTR_HI_ADDR,
    READ_PTR_LO_ADDR,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
)
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    KERNAL,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    RASTER_VBLANK_LINE,
    REU,
    SCREEN,
    halt_quantum_bytes,
)
from c64cast.video import modes_irq
from c64cast.video.modes_irq import (
    BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER_ADDR,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FRAME_TRACKER_ADDR,
    HOSTDMA_SWAP_IRQ_HANDLER,
    MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_BANK_SWAP_IRQ_HANDLER,
    MHIRES_FRAME_TRACKER_LEN,
    MHIRES_TRACKER_OFF_COLOR_REGS,
    REU_VIDEO_BITMAP_COLOR_LEN,
)

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


def worst_case_cycles(own: int) -> int:
    """`own` cycles of handler work and REU halt, stretched by everything that
    can take the bus while it runs.

    Audio NMIs at the fastest rate the streamer arms, each on the routine's
    short path but one on its longest (the ring wrap, once per 8 KB), and one
    host DMA halt as long as any the host sends while the streamer runs (its
    ring writes are cut to fit an NMI period; the frame tracker is shorter).
    Host writes are milliseconds apart on either link, far longer than this
    window, so one is all that can land in it. Sprites are left out: no
    bitmap scene enables them."""
    period = NMI_SAFE_MIN_PERIOD_CYCLES
    fast = nmi_cycles(RING_BUFFER_ADDR + 0x10)
    slow = max(nmi_cycles(RING_BUFFER_ADDR + 0xFF), nmi_cycles(RING_BUFFER_END - 1))
    halt = max(halt_quantum_bytes(period), MHIRES_FRAME_TRACKER_LEN)
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
# land before the first badline: the flip, or on mhires the first color-RAM
# chunk, which holds cell row 0's colors.
HANDLERS = (
    ("hires", BANK_SWAP_IRQ_HANDLER, partial(prime_reu, mhires=False), "dd00"),
    (
        "hires+pump",
        BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
        partial(prime_reu, mhires=False),
        "dd00",
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
        """Pins the measurement behind MHIRES_COMMIT_LAST_SAFE_LINE: line 45,
        which the other handlers keep, cannot carry the color copy."""
        events = run_commit(
            MHIRES_BANK_SWAP_IRQ_HANDLER, prime=partial(prime_reu, mhires=True), line=0
        )
        own = next(t for n, t in events if n == "chunk")
        self.assertLess(latest_safe_read_line(worst_case_cycles(own)), 45)


if __name__ == "__main__":
    unittest.main()
