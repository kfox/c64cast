"""hw/delivery.write_confirmed: a write is confirmed by the calling thread's
own losses, not by the backend-wide delivery_epoch."""

from __future__ import annotations

import threading
import unittest
from typing import cast

from _fakes import FakeAPI

from c64cast.hw.backend import C64Backend
from c64cast.hw.delivery import write_confirmed


class _CountingLink(FakeAPI):
    """FakeAPI counting writes and flushes. With ``foreign`` every flush is
    followed by a loss on another thread, as a render thread losing frames
    while the caller confirms; with ``own`` every write is a loss charged to
    the thread that issued it."""

    def __init__(self, *, foreign: bool = False, own: bool = False) -> None:
        super().__init__()
        self.foreign = foreign
        self.own = own
        self.attempts = 0
        self.flushes = 0

    def write(self) -> None:
        self.attempts += 1
        if self.own:
            self.delivery_epoch += 1

    def flush(self, timeout: float = 5.0) -> None:
        self.flushes += 1
        if self.foreign:
            render = threading.Thread(target=self._lose_a_frame)
            render.start()
            render.join()

    def _lose_a_frame(self) -> None:
        self.delivery_epoch += 1


class WriteConfirmedTest(unittest.TestCase):
    def test_another_threads_loss_does_not_unconfirm_the_write(self):
        link = _CountingLink(foreign=True)
        self.assertTrue(write_confirmed(cast(C64Backend, link), link.write))
        self.assertEqual((link.attempts, link.flushes), (1, 1))
        self.assertEqual(link.delivery_epoch, 1)

    def test_a_loss_of_the_callers_own_write_is_retried_then_refused(self):
        link = _CountingLink(own=True)
        self.assertFalse(write_confirmed(cast(C64Backend, link), link.write, tries=3))
        # A run whose write already counted a loss is not flushed.
        self.assertEqual((link.attempts, link.flushes), (3, 0))


if __name__ == "__main__":
    unittest.main()
