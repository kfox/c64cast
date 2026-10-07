"""A link outage longer than one DMA redial must not end the scene.

The render loop skips frames on a `LinkError` and keeps the scene active, so a
pulled cable costs frames rather than the show (c64cast#583)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import logging
import struct
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from test_playlist import FakeApi, FakeScene, _transition_factory
from test_socket_dma import _IDENT_REPLY, FakeSocket

from c64cast.app.playlist import LINK_OUTAGE_REPORT_S, Playlist, RenderLinkOutage
from c64cast.hw.backend import LinkError
from c64cast.hw.socket_dma import CMD_REUWRITE, SocketDMAError
from c64cast.video.modes_irq import push_mhires_via_reu


class _Link:
    """`socket.create_connection` for a machine whose cable can be pulled.

    While ``up``, each dial returns a fresh connection that answers the
    handshake's IDENTIFY. While down, a dial times out, as `create_connection`
    does when the far end is unplugged, and ``failed_dials`` counts it."""

    def __init__(self) -> None:
        self.up = True
        self.failed_dials = 0
        self.connections: list[FakeSocket] = []

    def dial(self, *_a: Any, **_kw: Any) -> FakeSocket:
        if not self.up:
            self.failed_dials += 1
            raise TimeoutError("timed out")
        sock = FakeSocket([_IDENT_REPLY])
        self.connections.append(sock)
        return sock

    def pull(self) -> None:
        """Pull the cable: the live connection resets, and dials time out."""
        self.up = False
        self.connections[-1].peer_reset = True


class _ReuVideoScene(FakeScene):
    """A REU-staged mhires video scene's frame: three REUWRITEs and the
    frame tracker, as `MHiresMode.push` sends them."""

    def __init__(self, api: Any) -> None:
        super().__init__("Video", frames_until_done=10_000)
        self.api = api
        self.pushed = 0

    def process_frame(self, current_time: float) -> bool:
        self.frame_count += 1
        push_mhires_via_reu(self.api, b"\x01" * 8000, b"\x02" * 1000, b"\x03" * 1000, 0, 0)
        self.pushed += 1
        return True


class RenderSurvivesDmaOutageTest(unittest.TestCase):
    """End to end over the real DMA client: a REU push whose link fails
    redial after redial, then comes back."""

    def setUp(self) -> None:
        from c64cast.hw.api import Ultimate64API

        self.link = _Link()
        dial = patch("c64cast.hw.socket_dma.socket.create_connection", side_effect=self.link.dial)
        dial.start()
        self.addCleanup(dial.stop)
        # Every frame redials, so each skipped frame is one failed redial.
        for name in ("REDIAL_BACKOFF_MIN_S", "REDIAL_BACKOFF_MAX_S"):
            p = patch(f"c64cast.hw.socket_dma.{name}", 0.0)
            p.start()
            self.addCleanup(p.stop)
        with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
            self.api = Ultimate64API("http://example.invalid")
        self.addCleanup(self.api.close)
        self.scene = _ReuVideoScene(self.api)
        self.pl = Playlist(
            [self.scene],
            self.api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )

    def _frame(self) -> None:
        self.pl.run_one_frame(self.scene, time.time())

    def test_frames_are_skipped_through_the_outage_and_the_scene_lives(self):
        self._frame()
        self.assertEqual(self.scene.pushed, 1)
        self.link.pull()
        with (
            self.assertLogs("c64cast.app.playlist", level="WARNING") as logs,
            self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"),
        ):
            for _ in range(5):
                self._frame()
        self.assertEqual(self.link.failed_dials, 5, "each skipped frame should redial once")
        self.assertEqual(self.scene.pushed, 1, "a frame pushed through a dead link")
        self.assertFalse(self.scene.is_done, "the outage ended the scene")
        self.assertIn("skipping frames until it answers", logs.output[0])
        self.assertEqual(len(logs.output), 1, "a short outage warns once")

        self.link.up = True
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            self._frame()
        self.assertIn("link back after", logs.output[-1])
        self.assertIn("5 frame(s) skipped", logs.output[-1])
        self.assertEqual(self.scene.pushed, 2)
        self.assertFalse(self.scene.is_done)
        sent = bytes(self.link.connections[-1].sent)
        self.assertIn(struct.pack("<H", CMD_REUWRITE), sent, "the frame after it never landed")

    def test_the_raise_is_a_link_error_not_a_bare_os_error(self):
        # The reconnect path's second send failure used to escape as the raw
        # OSError, which the render loop cannot tell from a defect.
        self._frame()
        sock = self.link.connections[-1]
        sock.fail_sendalls_remaining = 1  # this command's first send

        def dial_then_fail(*a: Any, **kw: Any) -> FakeSocket:
            fresh = self.link.dial(*a, **kw)
            real_sendall = fresh.sendall
            sends = {"n": 0}

            def sendall(data: bytes) -> None:
                sends["n"] += 1
                if sends["n"] == 2:  # the handshake's IDENTIFY goes through
                    raise BrokenPipeError("scripted failure")
                real_sendall(data)

            fresh.sendall = sendall  # type: ignore[method-assign]
            return fresh

        with (
            patch("c64cast.hw.socket_dma.socket.create_connection", side_effect=dial_then_fail),
            self.assertLogs("c64cast.hw.socket_dma", level="WARNING"),
            self.assertRaises(SocketDMAError) as raised,
        ):
            self.api.reu_write(0, b"\x00" * 16)
        self.assertIsInstance(raised.exception, LinkError)


class RenderLinkFailureTest(unittest.TestCase):
    """The playlist's side, with a scene that raises the link error itself."""

    def _playlist(self, scene: FakeScene) -> Playlist:
        return Playlist(
            [scene],
            FakeApi(),
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )

    def test_any_other_exception_still_ends_the_scene(self):
        scene = FakeScene("A", frames_until_done=100, raise_on_frame=1)
        pl = self._playlist(scene)
        with self.assertLogs("c64cast.app.playlist", level="ERROR"):
            pl.run_one_frame(scene, time.time())
        self.assertTrue(scene.is_done)

    def test_an_overlay_that_hits_the_dead_link_is_skipped_not_disabled(self):
        class Overlay:
            name = "clock"
            disabled = False
            calls = 0

            def process_frame(self, api: Any, scene: Any, t: float) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise SocketDMAError("did not answer the last redial")
                api.stats["writes"] += 1

            def is_busy(self) -> bool:
                return False

        scene = FakeScene("A", frames_until_done=100)
        ov = Overlay()
        scene.overlays = [ov]  # type: ignore[attr-defined]
        pl = self._playlist(scene)
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            pl.run_one_frame(scene, time.time())
        self.assertFalse(ov.disabled, "a link outage disabled the overlay for the scene")
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.run_one_frame(scene, time.time())
        self.assertEqual(ov.calls, 2)
        self.assertIn("1 frame(s) skipped", logs.output[-1])

    def test_a_frame_that_sends_nothing_does_not_end_the_outage(self):
        # A video between source frames returns True without sending anything,
        # so every other tick of a dead link raises nothing.
        class SkippingScene(FakeScene):
            land_writes = False

            def process_frame(self, current_time: float) -> bool:
                self.frame_count += 1
                if self.land_writes:
                    self.api.stats["writes"] += 1
                elif self.frame_count % 2:
                    raise SocketDMAError("did not answer the last redial")
                return True

        scene = SkippingScene("Video", frames_until_done=10_000)
        pl = self._playlist(scene)
        scene.api = pl.api  # type: ignore[attr-defined]
        pl.api.stats["writes"] = 100  # the link was up before the outage
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            for _ in range(6):
                pl.run_one_frame(scene, time.time())
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("WARNING", logs.output[0])
        self.assertTrue(pl.link_outage.active, "a frame that sent nothing ended the outage")

        scene.land_writes = True
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.run_one_frame(scene, time.time())
        self.assertIn("link back after", logs.output[-1])
        self.assertIn("3 frame(s) skipped", logs.output[-1])
        self.assertFalse(pl.link_outage.active)

    def test_a_frame_whose_writes_all_failed_does_not_end_the_outage(self):
        # `_emit` swallows a failed write: errors move, writes do not.
        class EmitFailScene(FakeScene):
            def process_frame(self, current_time: float) -> bool:
                self.frame_count += 1
                if self.frame_count == 1:
                    raise SocketDMAError("did not answer the last redial")
                self.api.stats["errors"] += 1
                self.api.stats["bytes"] += 1000
                return True

        scene = EmitFailScene("Picture", frames_until_done=10_000)
        pl = self._playlist(scene)
        scene.api = pl.api  # type: ignore[attr-defined]
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            for _ in range(3):
                pl.run_one_frame(scene, time.time())
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertTrue(pl.link_outage.active, "a frame whose writes all failed ended the outage")

    def test_a_write_that_lands_between_frames_ends_the_outage(self):
        # The next scene's setup() runs in _advance, outside any frame, and a
        # static scene's frames can then all hit the dirty cache.
        class Scene(FakeScene):
            fail = True

            def process_frame(self, current_time: float) -> bool:
                self.frame_count += 1
                if self.fail:
                    raise SocketDMAError("did not answer the last redial")
                return True

        scene = Scene("Video", frames_until_done=10_000)
        pl = self._playlist(scene)
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            pl.run_one_frame(scene, time.time())
        scene.fail = False
        pl.api.stats["writes"] += 5  # the next scene's setup writes landed
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.run_one_frame(scene, time.time())
        self.assertIn("link back after", logs.output[-1])
        self.assertFalse(pl.link_outage.active)

    def test_a_frame_where_scene_and_overlay_both_fail_counts_once(self):
        class Overlay:
            name = "clock"
            disabled = False

            def process_frame(self, api: Any, scene: Any, t: float) -> None:
                raise SocketDMAError("down")

            def is_busy(self) -> bool:
                return False

        class DownScene(FakeScene):
            def process_frame(self, current_time: float) -> bool:
                raise SocketDMAError("down")

        scene = DownScene("A")
        scene.overlays = [Overlay()]  # type: ignore[attr-defined]
        pl = self._playlist(scene)
        with self.assertLogs("c64cast.app.playlist", level="WARNING") as logs:
            pl.run_one_frame(scene, time.time())
        self.assertEqual(pl.link_outage.skipped, 1)
        self.assertIn("scene 'A'", logs.output[0])


class BlankSceneEndsThroughOutageTest(unittest.TestCase):
    """BlankScene decides its end after rendering, so a REU-staged blank
    scene whose push hits a dead link still has to end at its duration."""

    def test_a_reu_staged_blank_scene_ends_at_its_duration_while_the_link_is_down(self):
        from unittest.mock import MagicMock

        from c64cast.scenes.scenes import BlankScene
        from c64cast.video.modes.blank import BlankDisplayMode

        api = FakeApi()

        def reu_write(reu_offset: int, data: bytes) -> None:
            raise SocketDMAError("authentication was rejected on a previous attempt")

        api.reu_write = reu_write
        scene = BlankScene(api, None, BlankDisplayMode(use_reu_staged=True), MagicMock(), "Blank")
        scene.duration_s = 60.0
        scene.start_time = time.time()
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            pl.run_one_frame(scene, time.time())
        self.assertFalse(scene.is_done, "an outage inside the duration ended the scene")

        scene.start_time -= scene.duration_s
        pl.run_one_frame(scene, time.time())
        self.assertTrue(scene.is_done, "the dead link held the scene past its duration")


class BlockedConnectCountsTest(unittest.TestCase):
    """The first write to a machine that has gone away blocks for the connect
    timeout before it raises; that stretch is part of the outage (c64cast#618)."""

    def test_the_outage_is_timed_from_the_start_of_the_blocked_frame(self):
        now = [100.0]

        class BlockingScene(FakeScene):
            def process_frame(self, current_time: float) -> bool:
                self.frame_count += 1
                if self.frame_count == 1:
                    now[0] += 5.0  # the redial's connect timeout
                    raise SocketDMAError("did not answer the last redial")
                self.api.stats["writes"] += 1
                return True

        scene = BlockingScene("Video", frames_until_done=10_000)
        pl = Playlist(
            [scene],
            FakeApi(),
            target_fps=10.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        scene.api = pl.api  # type: ignore[attr-defined]
        pl.link_outage = RenderLinkOutage(pl.log, lambda: now[0])
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.run_one_frame(scene, time.time())
            pl.run_one_frame(scene, time.time())
        self.assertIn("link back after 5.0 s; 50 frame(s) skipped", logs.output[-1])


class RenderLinkOutageLogTest(unittest.TestCase):
    """A long outage keeps saying so; a recovered one says how long it was."""

    def test_reports_at_the_start_on_a_cadence_and_at_the_end(self):
        now = [100.0]
        outage = RenderLinkOutage(logging.getLogger("c64cast.app.playlist.t"), lambda: now[0])
        err = SocketDMAError("down")
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            outage.failed("scene 'A'", err, 7)
            now[0] += LINK_OUTAGE_REPORT_S - 0.1
            outage.failed("scene 'A'", err, 7)
            now[0] += 0.2
            outage.failed("scene 'A'", err, 7)
            now[0] += 5.0
            outage.frame_ok(7)  # nothing landed since the last failure
            self.assertTrue(outage.active)
            outage.frame_ok(8)
            outage.frame_ok(9)  # nothing more once it has ended
        self.assertEqual(len(logs.output), 3, logs.output)
        self.assertIn("WARNING", logs.output[0])
        self.assertIn("still down after 10 s, 3 frame(s) skipped", logs.output[1])
        self.assertIn("link back after 15.1 s; 3 frame(s) skipped", logs.output[2])
        self.assertFalse(outage.active)
        self.assertEqual(outage.skipped, 0)


if __name__ == "__main__":
    unittest.main()
