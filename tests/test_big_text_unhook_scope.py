"""big_text unhooks only the raster IRQ its own setup() hooked.

A clip dropped before launch, or a teardown run twice, must not unhook a
handler some other scene installed at `$0314`.
"""

# pyright: reportArgumentType=false
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from _fakes import FakeAPI

from c64cast.app.playlist import Playlist
from c64cast.hw.backend import LinkError
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


class _D016LinkDropAPI(FakeAPI):
    def write_regs(self, base, *vals):
        if str(base).upper() == "D016":
            raise LinkError("link dropped")
        super().write_regs(base, *vals)


class BigTextTeardownReleasesFollowersTest(unittest.TestCase):
    def test_a_failed_register_restore_still_ends_the_broadcast(self):
        api = _D016LinkDropAPI()
        overlay, scene = _overlay(), _scene()
        orchestrator = MagicMock()
        orchestrator.is_active.return_value = True
        scene._orchestrator = orchestrator
        scene._is_conductor = True
        scene._cfg = None
        overlay.setup(api, scene)
        with self.assertRaises(LinkError):
            overlay.teardown(api, scene)
        orchestrator.end.assert_called_once_with()

    def test_a_failing_end_leaves_the_link_error_primary(self):
        api = _D016LinkDropAPI()
        overlay, scene = _overlay(), _scene()
        orchestrator = MagicMock()
        orchestrator.is_active.return_value = True
        orchestrator.end.side_effect = RuntimeError("follower gone")
        scene._orchestrator = orchestrator
        scene._is_conductor = True
        scene._cfg = None
        overlay.setup(api, scene)
        with (
            self.assertLogs("c64cast.scenes.overlays.big_text", level="ERROR") as logs,
            self.assertRaises(LinkError),
        ):
            overlay.teardown(api, scene)
        self.assertTrue(any("follower gone" in m for m in logs.output))

    def test_a_failing_end_after_a_clean_restore_propagates(self):
        api = FakeAPI()
        overlay, scene = _overlay(), _scene()
        orchestrator = MagicMock()
        orchestrator.is_active.return_value = True
        orchestrator.end.side_effect = RuntimeError("follower gone")
        scene._orchestrator = orchestrator
        scene._is_conductor = True
        scene._cfg = None
        overlay.setup(api, scene)
        with self.assertRaises(RuntimeError):
            overlay.teardown(api, scene)


class SafeTeardownOfADisabledOverlayTest(unittest.TestCase):
    def test_a_disabled_overlay_still_unhooks_what_its_setup_hooked(self):
        api = FakeAPI()
        overlay, scene = _overlay(), _scene()
        scene.overlays = [overlay]
        overlay.setup(api, scene)
        overlay.disabled = True
        api.ops.clear()
        playlist = Playlist(
            scenes=[scene],
            api=api,
            target_fps=60.0,
            heartbeat_interval=999.0,
            stop_event=threading.Event(),
            key_poller=None,
        )
        playlist.safe_teardown(scene)
        self.assertIn("0314", _irq_writes(api))


if __name__ == "__main__":
    unittest.main()
