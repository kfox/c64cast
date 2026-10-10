"""An unconfirmed `$0314` restore is read back before it counts as failed.

A redial during the restore moves the loss mark whether or not the write
landed. Counted as lost, it leaves CIA #1 masked, and the keyboard dead, with
the vector already back on the kernal. The full retry rules of the shared
sequence are pinned in `tests/test_bank_swap_teardown.py`.
"""

from __future__ import annotations

import logging
import unittest
from typing import Any, cast

from _fakes import FakeAPI, lose_writes_to

from c64cast.app.config import InterstitialCfg
from c64cast.hw.backend import BackendCapabilityError, C64Backend
from c64cast.hw.c64 import CIA1, KERNAL, VECTORS
from c64cast.hw.irq_unhook import release_leaked_raster_irq, unhook_raster_irq
from c64cast.scenes.interstitial import InterstitialScene

_LOG = logging.getLogger("c64cast.test_irq_unhook")
_KERNAL = bytes([KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF])
_UNMASKED = f"{CIA1.ICR_ENABLE_TIMER_A:02X}"
_MASKED = f"{CIA1.ICR_DISABLE_ALL:02X}"


class _RedialDuringRestoreAPI(FakeAPI):
    """Every `$0314` write lands, but moves the loss mark as a redial does.
    `reads` is what a read of `$0314` returns (a callable may raise)."""

    def __init__(self, reads: Any, *, answers: bool = True) -> None:
        super().__init__()
        self.reads = reads
        self.answers = answers
        self.vector_reads = 0

    def write_regs(self, base, *vals):
        super().write_regs(base, *vals)
        if str(base).upper() == f"{VECTORS.IRQ:04X}":
            self.delivery_epoch += 1

    def link_answers(self) -> bool:
        return self.answers

    def read_memory(self, address, length, timeout=1.0):
        if address != VECTORS.IRQ:
            return super().read_memory(address, length, timeout)
        self.vector_reads += 1
        return self.reads() if callable(self.reads) else self.reads


def _unhook(api: FakeAPI) -> bool:
    return unhook_raster_irq(cast(C64Backend, api), _LOG, "test")


class RestoreReadbackTest(unittest.TestCase):
    def test_a_restore_that_reads_back_as_the_kernal_unmasks_cia1(self):
        api = _RedialDuringRestoreAPI(_KERNAL)
        with self.assertLogs(_LOG, level="WARNING") as logs:
            self.assertTrue(_unhook(api))
        self.assertEqual(api.memories["DC0D"], _UNMASKED)
        self.assertEqual(api.vector_reads, 1)
        self.assertTrue(any("reads back as $EA31" in m for m in logs.output))

    def test_a_vector_still_on_the_handler_leaves_cia1_masked(self):
        api = _RedialDuringRestoreAPI(bytes([0x00, 0xC5]))
        with self.assertLogs(_LOG, level="ERROR"):
            self.assertFalse(_unhook(api))
        self.assertEqual(api.memories["DC0D"], _MASKED)

    def test_no_read_is_made_over_a_link_that_does_not_answer(self):
        api = _RedialDuringRestoreAPI(_KERNAL, answers=False)
        with self.assertLogs(_LOG, level="ERROR"):
            self.assertFalse(_unhook(api))
        self.assertEqual(api.vector_reads, 0)
        self.assertEqual(api.memories["DC0D"], _MASKED)

    def test_a_read_that_fails_leaves_cia1_masked(self):
        for failure in (None, BackendCapabilityError("read_memory"), OSError("refused")):
            with self.subTest(failure=repr(failure)):

                def read(failure: Any = failure) -> Any:
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                api = _RedialDuringRestoreAPI(read)
                with self.assertLogs(_LOG, level="ERROR"):
                    self.assertFalse(_unhook(api))
                self.assertEqual(api.memories["DC0D"], _MASKED)

    def test_a_defect_in_the_read_is_logged_as_the_step_failing(self):
        def read() -> Any:
            raise TypeError("a broken backend")

        api = _RedialDuringRestoreAPI(read)
        with self.assertLogs(_LOG, level="ERROR") as logs:
            self.assertFalse(_unhook(api))
        self.assertEqual(api.memories["DC0D"], _MASKED)
        self.assertTrue(any("a broken backend" in m for m in logs.output))

    def test_a_confirmed_restore_reads_nothing(self):
        api = _RedialDuringRestoreAPI(_KERNAL)
        api.write_regs = FakeAPI.write_regs.__get__(api)  # type: ignore[method-assign]
        self.assertTrue(_unhook(api))
        self.assertEqual(api.vector_reads, 0)

    def test_the_card_rearms_the_keyboard_after_a_redial_during_its_restore(self):
        api = _RedialDuringRestoreAPI(_KERNAL)
        scene = InterstitialScene(cast(C64Backend, api), "Next", InterstitialCfg(background="none"))
        with self.assertLogs("c64cast.scenes.interstitial", level="WARNING"):
            scene.setup()
        self.assertEqual(api.memories["DC0D"], _UNMASKED)


class ReleaseUnmaskTest(unittest.TestCase):
    def test_a_release_whose_unmask_is_lost_does_not_count_as_landed(self):
        # The playlist keeps the release owed on False; a True here would leave
        # the keyboard dead for the scene with the vector already restored.
        api = FakeAPI()
        lose_writes_to(api, CIA1.ICR)
        with self.assertLogs(_LOG, level="ERROR"):
            self.assertFalse(release_leaked_raster_irq(cast(C64Backend, api), _LOG, "test"))
        self.assertEqual(api.regs[f"{VECTORS.IRQ:04X}"], tuple(_KERNAL))

    def test_a_release_that_lands_its_unmask_counts_as_landed(self):
        api = FakeAPI()
        self.assertTrue(release_leaked_raster_irq(cast(C64Backend, api), _LOG, "test"))
        self.assertEqual(api.memories["DC0D"], _UNMASKED)


if __name__ == "__main__":
    unittest.main()
