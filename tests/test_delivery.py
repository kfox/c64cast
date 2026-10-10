"""hw/delivery.write_confirmed: a write is confirmed by the calling thread's
own losses, not by the backend-wide delivery_epoch."""

from __future__ import annotations

import unittest
from typing import Any, cast

from c64cast.hw.delivery import write_confirmed


class _Link:
    """Counts writes and flushes. ``foreign`` moves ``delivery_epoch`` at every
    flush, as a render thread losing frames does; ``own`` charges every write
    to the caller's loss mark."""

    def __init__(self, *, foreign: bool = False, own: bool = False) -> None:
        self.foreign = foreign
        self.own = own
        self.delivery_epoch = 0
        self.mark = 0
        self.writes = 0
        self.flushes = 0

    def write_loss_mark(self) -> int:
        return self.mark

    def writes_lost_since(self, mark: int) -> bool:
        return self.mark != mark

    def write(self) -> None:
        self.writes += 1
        if self.own:
            self.mark += 1
            self.delivery_epoch += 1

    def flush(self) -> None:
        self.flushes += 1
        if self.foreign:
            self.delivery_epoch += 1


class WriteConfirmedTest(unittest.TestCase):
    def test_another_threads_loss_does_not_unconfirm_the_write(self):
        link = _Link(foreign=True)
        self.assertTrue(write_confirmed(cast(Any, link), link.write))
        self.assertEqual((link.writes, link.flushes), (1, 1))

    def test_a_loss_of_the_callers_own_write_is_retried_then_refused(self):
        link = _Link(own=True)
        self.assertFalse(write_confirmed(cast(Any, link), link.write, tries=3))
        # A run whose write already counted a loss is not flushed.
        self.assertEqual((link.writes, link.flushes), (3, 0))


if __name__ == "__main__":
    unittest.main()
