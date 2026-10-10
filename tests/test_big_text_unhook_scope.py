"""big_text unhooks only the raster IRQ its own setup() hooked.

A clip dropped before launch, or a teardown run twice, must not unhook a
handler some other scene installed at `$0314`.
"""

# pyright: reportArgumentType=false
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from _fakes import FakeAPI

from c64cast.scenes.overlays.big_text import BigTextOverlay
from c64cast.video.modes import BlankDisplayMode

_IRQ_WRITES = {"0314", "D01A", "DC0D", "D016"}


def _overlay() -> BigTextOverlay:
    return BigTextOverlay(messages=[{"text": "HI"}], charset_path="")


def _scene() -> MagicMock:
    scene = MagicMock()
    scene.display_mode = BlankDisplayMode()
    return scene


def _irq_writes(api: FakeAPI) -> list[str]:
    return [
        op[1] for op in api.ops if op[0] in ("write_memory", "write_regs") and op[1] in _IRQ_WRITES
    ]


class BigTextUnhookScopeTest(unittest.TestCase):
    def test_a_never_set_up_overlay_unhooks_nothing(self):
        api = FakeAPI()
        _overlay().teardown(api, _scene())
        self.assertEqual(_irq_writes(api), [])

    def test_a_second_teardown_unhooks_nothing(self):
        api = FakeAPI()
        overlay, scene = _overlay(), _scene()
        overlay.setup(api, scene)
        overlay.teardown(api, scene)
        self.assertIn("0314", _irq_writes(api))
        api.ops.clear()
        overlay.teardown(api, scene)
        self.assertEqual(_irq_writes(api), [])


if __name__ == "__main__":
    unittest.main()
