"""A host-DMA page flip's next frame waits for the C64 to commit the last one
(c64cast#834).

The swap tracker write returns when the kernel has taken the bytes, and the
IRQ commits the flip on a later field, so a push that trusts its own record of
the displayed bank writes the next frame into the bank still on screen. Where
the machine can report the commit (the Ultimate 64's $D7FF, read over socket
DMA), the push waits for it; elsewhere it keeps the old behavior.
"""

from __future__ import annotations

import unittest
from typing import cast
from unittest import mock

import numpy as np
from _fakes import FakeAPI, quiet_logging

from c64cast.hw.backend import BackendCapabilityError, C64Backend, HardwareProfile, LinkError
from c64cast.hw.c64 import VIC_BANK_0, VIC_BANK_2
from c64cast.video.modes import bitmap
from c64cast.video.modes.hires import HiresDisplayMode
from c64cast.video.modes.mhires import MultiHiresDisplayMode
from c64cast.video.modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    COMMIT_REPORT_ADDR,
    DD00_BANK_0,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_SWAP_REPORTING_IRQ_HANDLER,
    FRAME_TRACKER_ADDR,
    HOSTDMA_SWAP_IRQ_HANDLER,
    HOSTDMA_SWAP_REPORTING_IRQ_HANDLER,
    HOSTDMA_TRACKER_OFF_BANK,
    HOSTDMA_TRACKER_OFF_READY,
)

_BANK_PAGES = {
    DD00_BANK_0: {VIC_BANK_0.BITMAP, VIC_BANK_0.SCREEN, VIC_BANK_0.SCREEN_ALT},
    DD00_BANK_2: {VIC_BANK_2.BITMAP, VIC_BANK_2.SCREEN, VIC_BANK_2.SCREEN_ALT},
}
_REPORTING = (HOSTDMA_SWAP_REPORTING_IRQ_HANDLER, FLICKER_SWAP_REPORTING_IRQ_HANDLER)
_HANDLER_KEY = f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"
_TRACKER_KEY = f"{FRAME_TRACKER_ADDR:04X}"


class PageFlipC64(FakeAPI):
    """A FakeAPI with the C64 side of a page flip.

    The swap IRQ commits an armed flip on the `commit_after_reads`th debug
    register read after the arm, standing in for the fields that pass while
    the host polls; `commits=False` is a C64 whose IRQ never runs. Every
    bitmap or screen write that lands in the bank on screen is recorded."""

    def __init__(
        self,
        *,
        reports: bool = True,
        commits: bool = True,
        commit_after_reads: int = 2,
        stale_register: int = 0x00,
    ):
        super().__init__()
        self.profile = HardwareProfile(
            name="Fake U64", family="fake", supports_debug_register=reports
        )
        self.commits = commits
        self.commit_after_reads = commit_after_reads
        self.debug_register = stale_register
        self.shown_dd00: int | None = None
        self.reads = 0
        self._reads_since_arm = 0
        self.into_shown_bank: list[int] = []

    @property
    def backend(self) -> C64Backend:
        return cast(C64Backend, self)

    def write_memory(self, addr, data_hex):
        super().write_memory(addr, data_hex)
        if int(addr, 16) == 0xDD00:
            self.shown_dd00 = int(data_hex, 16)
        elif int(addr, 16) == COMMIT_REPORT_ADDR:
            self.debug_register = int(data_hex, 16)

    def write_memory_file(self, addr, data):
        super().write_memory_file(addr, data)
        if int(addr, 16) == FRAME_TRACKER_ADDR:
            self._reads_since_arm = 0

    def write_region(self, addr, data, region_id=None):
        if self.shown_dd00 is not None and addr in _BANK_PAGES[self.shown_dd00]:
            self.into_shown_bank.append(addr)
        return super().write_region(addr, data, region_id)

    def read_debug_register(self) -> int:
        if not self.profile.supports_debug_register:
            raise BackendCapabilityError("read_debug_register")
        self.reads += 1
        self._reads_since_arm += 1
        if self.commits and self._reads_since_arm >= self.commit_after_reads:
            self._vblank()
        return self.debug_register

    def _vblank(self) -> None:
        tracker = bytearray(self.mem_files.get(_TRACKER_KEY, b""))
        if len(tracker) <= HOSTDMA_TRACKER_OFF_READY or not tracker[HOSTDMA_TRACKER_OFF_READY]:
            return
        self.shown_dd00 = tracker[HOSTDMA_TRACKER_OFF_BANK]
        if self.mem_files.get(_HANDLER_KEY) in _REPORTING:
            self.debug_register = self.shown_dd00
        tracker[HOSTDMA_TRACKER_OFF_READY] = 0
        self.mem_files[_TRACKER_KEY] = bytes(tracker)


def _frame(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (200, 320, 3), dtype=np.uint8)


def _page_flip_modes():
    return (
        ("flicker hires", lambda: HiresDisplayMode("normal", flicker_tolerance="clean")),
        ("plain hires", lambda: HiresDisplayMode("normal", double_buffer=True)),
        (
            "flicker mhires",
            lambda: MultiHiresDisplayMode(flicker_tolerance="clean", perceptual=True),
        ),
        ("plain mhires", lambda: MultiHiresDisplayMode(double_buffer=True)),
    )


class _FakeClock:
    """bitmap's `time`, advanced only by its own sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _PageFlipTest(unittest.TestCase):
    quiet = True

    def setUp(self) -> None:
        if self.quiet:
            self.enterContext(quiet_logging())
        self.clock = _FakeClock()
        self.enterContext(mock.patch.object(bitmap, "time", self.clock))

    def _push_frames(self, mode, api: PageFlipC64, frames: int = 4) -> None:
        mode.setup(api.backend)
        for seed in range(frames):
            mode.push(api.backend, mode.compose(_frame(seed)))


class WaitsForTheCommitTest(_PageFlipTest):
    def test_no_frame_is_written_into_the_bank_on_screen(self):
        for name, make in _page_flip_modes():
            with self.subTest(mode=name):
                api = PageFlipC64()
                self._push_frames(make(), api)
                self.assertEqual(api.into_shown_bank, [])
                self.assertGreater(api.reads, 0)

    def test_installs_the_reporting_handler_and_seeds_the_register(self):
        for name, make in _page_flip_modes():
            with self.subTest(mode=name):
                api = PageFlipC64()
                make().setup(api.backend)
                self.assertIn(api.mem_files[_HANDLER_KEY], _REPORTING)
                self.assertEqual(api.debug_register, DD00_BANK_0)

    def test_a_register_left_at_the_first_flips_bank_does_not_pass_for_its_commit(self):
        """An earlier scene can leave $D7FF holding $95, the value the first
        flip of this one arms."""
        for name, make in _page_flip_modes():
            with self.subTest(mode=name):
                api = PageFlipC64(stale_register=DD00_BANK_2)
                self._push_frames(make(), api)
                self.assertEqual(api.into_shown_bank, [])

    def test_a_scene_set_up_again_does_not_wait_for_its_last_activations_flip(self):
        """A playlist loop sets the same mode up again after teardown, and
        the flip its last push armed is gone with the tracker that held it."""
        api = PageFlipC64(commits=False)
        mode = HiresDisplayMode("normal", double_buffer=True)
        mode.setup(api.backend)
        mode.push(api.backend, mode.compose(_frame(0)))
        mode.teardown(api.backend)
        mode.setup(api.backend)
        mode.push(api.backend, mode.compose(_frame(1)))
        self.assertEqual(self.clock.now, 0.0, "the first push after setup waited")

    def test_a_link_failure_while_waiting_skips_the_frame_before_any_write(self):
        """The playlist skips a frame that raises a LinkError; the frame must
        not have written anything, and the next one still waits."""
        api = PageFlipC64(commit_after_reads=1)
        mode = HiresDisplayMode("normal", double_buffer=True)
        mode.setup(api.backend)
        mode.push(api.backend, mode.compose(_frame(0)))
        ops_before = len(api.ops)
        with mock.patch.object(api, "read_debug_register", side_effect=LinkError("down")):
            with self.assertRaises(LinkError):
                mode.push(api.backend, mode.compose(_frame(1)))
        self.assertEqual(api.ops[ops_before:], [])
        mode.push(api.backend, mode.compose(_frame(2)))
        self.assertEqual(api.into_shown_bank, [])


class WithoutTheCapabilityTest(_PageFlipTest):
    """The Ultimate II+ and the TeensyROM+ cannot read the commit back, so
    they keep the plain handlers and never wait."""

    def test_keeps_the_plain_handlers_and_never_reads(self):
        plain = (HOSTDMA_SWAP_IRQ_HANDLER, FLICKER_SWAP_IRQ_HANDLER)
        for name, make in _page_flip_modes():
            with self.subTest(mode=name):
                api = PageFlipC64(reports=False)
                self._push_frames(make(), api)
                self.assertIn(api.mem_files[_HANDLER_KEY], plain)
                self.assertEqual(api.reads, 0)
                self.assertNotIn(f"{COMMIT_REPORT_ADDR:04X}", api.memories)
                self.assertEqual(self.clock.now, 0.0, "a push waited")


class CommitTimeoutTest(_PageFlipTest):
    quiet = False

    def test_writes_the_frame_anyway_and_warns_once(self):
        api = PageFlipC64(commits=False)
        mode = HiresDisplayMode("normal", double_buffer=True)
        mode.setup(api.backend)
        mode.push(api.backend, mode.compose(_frame(0)))
        with self.assertLogs("c64cast.video.modes.bitmap", "WARNING") as logs:
            # The seed reads as bank 0 committed, so the flips to bank 2 are
            # the ones that time out: the second push's and the fourth's.
            for seed in range(1, 4):
                mode.push(api.backend, mode.compose(_frame(seed)))
        self.assertEqual(len(logs.records), 1, [r.getMessage() for r in logs.records])
        self.assertIn("did not report a page flip", logs.records[0].getMessage())
        # Both frames were written: the wait gave up rather than skipping.
        self.assertIn(VIC_BANK_0.BITMAP, api.regions)
        self.assertGreaterEqual(self.clock.now, 2 * bitmap.COMMIT_WAIT_TIMEOUT_S)
        self.assertLess(self.clock.now, 2 * bitmap.COMMIT_WAIT_TIMEOUT_S + 0.01)


class MirrorFollowsTheReportingHandlersTest(_PageFlipTest):
    """Preview and recording read the bank and the flicker page from the
    handler bytes they see go out, so they must know the reporting ones."""

    def _mirror(self, api: FakeAPI):
        from c64cast.video.framebuffer import Framebuffer

        fb = Framebuffer()
        for op, addr, *rest in api.ops:
            if op == "write_memory":
                fb.on_write(int(addr, 16), bytes.fromhex(rest[0]))
            elif op == "write_memory_file":
                fb.on_write(int(addr, 16), rest[0])
            elif op == "write_regs":
                fb.on_write(int(addr, 16), bytes(v & 0xFF for v in rest[0]))
            elif op == "write_region":
                fb.on_write(addr, rest[0])
        return fb, bytes(fb.ram)

    def test_follows_the_bank_a_reporting_flip_arms(self):
        api = PageFlipC64()
        mode = HiresDisplayMode("normal", double_buffer=True)
        self._push_frames(mode, api, frames=1)
        fb, ram = self._mirror(api)
        self.assertEqual(fb._vic_bank_base(ram), VIC_BANK_2.BASE)

    def test_finds_the_second_page_under_a_reporting_flicker_handler(self):
        api = PageFlipC64()
        mode = HiresDisplayMode("normal", flicker_tolerance="clean")
        self._push_frames(mode, api, frames=1)
        fb, ram = self._mirror(api)
        self.assertEqual(fb._flicker_page_b(ram, VIC_BANK_2.BASE), VIC_BANK_2.SCREEN_ALT)


if __name__ == "__main__":
    unittest.main()
