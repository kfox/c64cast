"""A link outage longer than one DMA redial must not end the scene.

The render loop skips frames on a `LinkError` and keeps the scene active, so a
pulled cable costs frames rather than the show (c64cast#583)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import logging
import os
import random
import struct
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

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
        # A still clock: these frames run far over a 10000 fps budget, and
        # the outage would charge each one for the slots it overran.
        self.pl.link_outage = RenderLinkOutage(self.pl.log, lambda: 0.0)

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
        pl = Playlist(
            [scene],
            FakeApi(),
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        # A still clock, as in RenderSurvivesDmaOutageTest.
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)
        return pl

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


class _OutageApi(FakeApi):
    """A FakeApi whose link stays down for `down_probes` calls to
    `link_answers`, then answers. `on_probe` runs on every probe."""

    def __init__(self, down_probes: int) -> None:
        super().__init__()
        self.down_probes = down_probes
        self.probes = 0
        self.on_probe: Any = None

    def link_answers(self) -> bool:
        self.probes += 1
        if self.on_probe is not None:
            self.on_probe()
        if self.probes <= self.down_probes:
            return False
        self.stats["writes"] += 1  # the probe's own round trip landed
        return True


class _LossySetupScene(FakeScene):
    """A setup that loses its writes (moves `delivery_epoch` or raises)
    on its first `lossy_setups` runs and lands every one after."""

    def __init__(self, api: Any, lossy_setups: int, *, raise_it: bool = False) -> None:
        super().__init__("B", frames_until_done=10_000)
        self.api = api
        self.lossy_setups = lossy_setups
        self.raise_it = raise_it

    def setup(self) -> None:
        super().setup()
        if self.setup_count <= self.lossy_setups:
            if self.raise_it:
                raise SocketDMAError("did not answer the last redial")
            self.api.delivery_epoch += 1
            self.api.stats["errors"] += 1
        else:
            self.api.stats["writes"] += 3


class SetupThroughOutageTest(unittest.TestCase):
    """A scene set up while the link is down waits for the link and sets up
    again, rather than playing a setup that never reached the machine or
    ending the run (c64cast#609)."""

    def setUp(self) -> None:
        p = patch("c64cast.app.playlist.SETUP_RETRY_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def _playlist(self, api: FakeApi, scene: FakeScene) -> Playlist:
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)
        return pl

    def _assert_waited_then_set_up_again(self, raise_it: bool) -> None:
        api = _OutageApi(down_probes=4)
        scene = _LossySetupScene(api, lossy_setups=1, raise_it=raise_it)
        pl = self._playlist(api, scene)
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.safe_setup(scene)
        self.assertEqual(api.probes, 5, "the setup did not wait for the link")
        self.assertEqual(scene.setup_count, 2)
        self.assertEqual(scene.teardown_count, 1, "the failed setup was not torn down first")
        self.assertEqual(scene.keep_pick_count, 1, "the retry did not keep the scene's pick")
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn("setup of 'B'", warnings[0])
        self.assertIn("link back after", logs.output[-1])
        self.assertFalse(pl.link_outage.active)

    def test_a_setup_whose_writes_were_lost_waits_and_sets_up_again(self):
        self._assert_waited_then_set_up_again(raise_it=False)

    def test_a_setup_that_raises_a_link_error_waits_and_sets_up_again(self):
        self._assert_waited_then_set_up_again(raise_it=True)

    def test_a_clean_setup_runs_once_and_asks_the_link_nothing(self):
        api = _OutageApi(down_probes=0)
        scene = _LossySetupScene(api, lossy_setups=0)
        self._playlist(api, scene).safe_setup(scene)
        self.assertEqual((scene.setup_count, scene.teardown_count, api.probes), (1, 0, 0))

    def test_a_link_that_answers_but_keeps_losing_writes_is_retried_a_bounded_number_of_times(
        self,
    ):
        from c64cast.app.playlist import SETUP_LOSSY_TRIES

        api = _OutageApi(down_probes=0)
        scene = _LossySetupScene(api, lossy_setups=10_000)
        pl = self._playlist(api, scene)
        with self.assertLogs("c64cast.app.playlist", level="WARNING") as logs:
            pl.safe_setup(scene)
        self.assertEqual(scene.setup_count, SETUP_LOSSY_TRIES)
        self.assertEqual(scene.teardown_count, SETUP_LOSSY_TRIES - 1)
        self.assertIn("keeping it as set up", logs.output[-1])

    def test_a_stop_while_waiting_ends_the_run_without_rendering_the_scene(self):
        api = _OutageApi(down_probes=10_000)
        scene = _LossySetupScene(api, lossy_setups=10_000)
        pl = self._playlist(api, scene)

        def stop_after_a_few() -> None:
            if api.probes >= 3:
                pl.stop_event.set()

        api.on_probe = stop_after_a_few
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            pl.run()
        self.assertEqual(scene.setup_count, 1)
        self.assertEqual(scene.frame_count, 0, "a scene that never set up rendered a frame")
        self.assertEqual(scene.teardown_count, 1, "the run's teardown missed the scene")

    def test_a_lossy_retry_waits_before_it_sets_up_again(self):
        api = _OutageApi(down_probes=0)
        scene = _LossySetupScene(api, lossy_setups=10_000)
        pl = self._playlist(api, scene)
        with (
            patch("c64cast.app.playlist.SETUP_RETRY_S", 0.001),
            patch.object(pl.stop_event, "wait", wraps=pl.stop_event.wait) as wait,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl.safe_setup(scene)
        self.assertEqual(scene.setup_count, 3)
        self.assertEqual(
            [c.args for c in wait.call_args_list],
            [(0.001,), (0.001,)],
            "a lossy retry did not wait SETUP_RETRY_S",
        )

    def test_a_stop_before_a_lossy_retry_ends_the_setup(self):
        api = _OutageApi(down_probes=0)
        scene = _LossySetupScene(api, lossy_setups=10_000)
        pl = self._playlist(api, scene)
        pl.stop_event.set()
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            set_up = pl._setup_through_outage(scene)
        self.assertFalse(set_up)
        self.assertEqual((scene.setup_count, scene.teardown_count), (1, 0))

    def test_the_wait_for_the_link_counts_as_skipped_frames(self):
        api = _OutageApi(down_probes=4)
        scene = _LossySetupScene(api, lossy_setups=1)
        pl = self._playlist(api, scene)
        ticks = iter(range(1000))
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 10.0 * next(ticks))
        pl.frame_time_for = lambda _scene: 1.0  # type: ignore[method-assign]
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.safe_setup(scene)
        skipped = int(logs.output[-1].split("; ")[1].split(" ")[0])
        # The clock moves 10 s per read and a frame is 1 s: the failed attempt
        # holds 10 frames, and each of the three waits that end in an
        # unanswered ask holds 10 more.
        self.assertEqual(skipped, 40, logs.output)

    def test_a_teardown_that_raises_before_the_retry_does_not_end_the_setup(self):
        api = _OutageApi(down_probes=1)
        scene = _LossySetupScene(api, lossy_setups=1)
        scene.raise_on_teardown = True
        pl = self._playlist(api, scene)
        with self.assertLogs("c64cast.app.playlist", level="ERROR") as logs:
            self.assertTrue(pl._setup_through_outage(scene))
        self.assertEqual(scene.setup_count, 2)
        self.assertIn("before its setup retry failed", logs.output[0])

    def test_a_link_error_from_the_palette_settle_waits_and_sets_up_again(self):
        api = _OutageApi(down_probes=1)
        scene = _LossySetupScene(api, lossy_setups=0)
        pl = self._playlist(api, scene)
        settle = patch(
            "c64cast.app.playlist.hardware_palette.settle_for",
            side_effect=[SocketDMAError("did not answer"), None],
        )
        with settle, self.assertLogs("c64cast.app.playlist", level="INFO"):
            self.assertTrue(pl._setup_through_outage(scene))
        self.assertEqual(scene.setup_count, 1)
        self.assertEqual(api.probes, 2)


class BackendLinkAnswersTest(unittest.TestCase):
    def test_a_backend_with_no_round_trip_of_its_own_says_the_link_answers(self):
        from c64cast.hw.teensyrom_api import TeensyROMBackend

        self.assertTrue(TeensyROMBackend.link_answers(MagicMock()))


class SetupRetryKeepsThePickTest(unittest.TestCase):
    """A setup run again after the link cost it writes plays the file the
    "UP NEXT" card named, not a new random pick (c64cast#609)."""

    def setUp(self) -> None:
        p = patch("c64cast.app.playlist.SETUP_RETRY_S", 0.0)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _files(self, ext: str, n: int = 16) -> None:
        for i in range(n):
            open(os.path.join(self.tmp.name, f"f{i:02d}{ext}"), "wb").close()

    def test_a_slideshow_set_up_again_opens_on_the_slide_the_card_named(self):
        import cv2
        import numpy as np

        from c64cast.scenes.scenes import SlideshowScene

        img = np.zeros((4, 4, 3), dtype=np.uint8)
        for i in range(16):
            cv2.imwrite(os.path.join(self.tmp.name, f"s{i:02d}.png"), img)
        mode = MagicMock()
        mode.default_target_fps = None
        scene = SlideshowScene(MagicMock(), mode, self.tmp.name)
        random.seed(609)
        scene.prepare_next()
        card = scene._current_path
        api = _OutageApi(down_probes=0)
        real_setup = scene.setup
        opened: list[str | None] = []

        def setup_losing_the_first() -> None:
            real_setup()
            opened.append(scene._current_path)
            if len(opened) == 1:
                api.delivery_epoch += 1
                api.stats["errors"] += 1

        scene.setup = setup_losing_the_first  # type: ignore[method-assign]
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            self.assertTrue(pl._setup_through_outage(scene))
        self.assertEqual(opened, [card, card])

    def test_a_video_keeps_its_pick_and_rolls_again_after_a_failed_one(self):
        from c64cast.scenes.scenes import VideoScene

        self._files(".mp4")
        scene = VideoScene(MagicMock(), None, MagicMock(), self.tmp.name)
        scene.prepare_next()
        scene._prepared = False  # what setup() does with the pick
        scene.keep_pick_for_resetup()
        self.assertTrue(scene._prepared)
        for name in os.listdir(self.tmp.name):
            os.remove(os.path.join(self.tmp.name, name))
        with self.assertLogs("c64cast.scenes.scenes", level="ERROR"):
            self.assertFalse(scene._pick_filepath())
        scene.keep_pick_for_resetup()
        self.assertFalse(scene._prepared)

    def test_a_sid_scene_keeps_its_tune_and_rolls_again_after_a_failed_pick(self):
        from _fakes import bare_waveform_scene

        header = MagicMock(name="hdr")
        header.name = "Tune"
        scene = bare_waveform_scene(
            _candidates=["a.sid", "b.sid"],
            _prepared=False,
            song=1,
            header=header,
            _sid_file="a.sid",
            _explicit_duration_s=None,
        )
        scene._adopt_live_duration = lambda: None  # type: ignore[method-assign]
        scene._pick_and_load_sid = lambda: None  # type: ignore[method-assign]
        scene._resolve_duration_for_current_sid = lambda: 42.0  # type: ignore[method-assign]
        self.assertTrue(scene._repick_sid())
        scene.keep_pick_for_resetup()
        self.assertTrue(scene._prepared)

        def no_tune_loads() -> None:
            raise ValueError("no candidate could be loaded")

        scene._prepared = False
        scene._pick_and_load_sid = no_tune_loads  # type: ignore[method-assign]
        with self.assertLogs("c64cast.sid.waveform", level="ERROR"):
            self.assertFalse(scene._repick_sid())
        scene.keep_pick_for_resetup()
        self.assertFalse(scene._prepared)


class _Flaky(_Link):
    """A `_Link` that comes back up after `down_dials` failed dials."""

    def __init__(self, down_dials: int) -> None:
        super().__init__()
        self.down_dials = down_dials

    def dial(self, *a: Any, **kw: Any) -> FakeSocket:
        if not self.up and self.failed_dials >= self.down_dials:
            self.up = True
        sock = super().dial(*a, **kw)
        # The handshake's IDENTIFY, then the link probes' round trips.
        sock._replies.extend([_IDENT_REPLY] * 8)
        return sock


class _RegisterScene(FakeScene):
    """A setup that pokes the VIC registers, as every display mode's does."""

    def __init__(self, api: Any) -> None:
        super().__init__("B", frames_until_done=10_000)
        self.api = api

    def setup(self) -> None:
        super().setup()
        self.api.write_regs("D020", 0, 0)


class SetupSurvivesDmaOutageTest(unittest.TestCase):
    """End to end over the real DMA client: the next scene's setup runs with
    the cable pulled, and the scene sets up again once it is back."""

    def test_the_setup_waits_out_the_outage_and_lands_once_the_link_is_back(self):
        from c64cast.hw.api import Ultimate64API

        link = _Flaky(down_dials=3)
        for target, value in (
            ("c64cast.hw.socket_dma.socket.create_connection", link.dial),
            ("c64cast.hw.socket_dma.REDIAL_BACKOFF_MIN_S", 0.0),
            ("c64cast.hw.socket_dma.REDIAL_BACKOFF_MAX_S", 0.0),
            ("c64cast.app.playlist.SETUP_RETRY_S", 0.0),
        ):
            p = patch(target, side_effect=value) if callable(value) else patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
            api = Ultimate64API("http://example.invalid")
        self.addCleanup(api.close)
        scene = _RegisterScene(api)
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)
        api.write_regs("D020", 1)
        link.pull()
        with (
            self.assertLogs("c64cast.app.playlist", level="INFO") as logs,
            self.assertLogs("c64cast.hw", level="DEBUG"),
        ):
            pl.safe_setup(scene)
        self.assertTrue(link.up)
        self.assertEqual(scene.setup_count, 2)
        self.assertEqual(scene.teardown_count, 1)
        self.assertIn("link back after", logs.output[-1])
        sent = bytes(link.connections[-1].sent)
        self.assertIn(b"\x20\xd0\x00\x00", sent, "the second setup's writes never landed")


class _ResettingScene(FakeScene):
    """A setup whose DMA connection resets once on every run, on a link that
    redials at once, either between its two writes or after the last one."""

    def __init__(self, api: Any, link: _Link, *, after_last_write: bool) -> None:
        super().__init__("B", frames_until_done=10_000)
        self.api = api
        self.link = link
        self.after_last_write = after_last_write
        self.on_setup: Any = None

    def setup(self) -> None:
        super().setup()
        if self.on_setup is not None:
            self.on_setup()
        self.api.write_regs("D020", 0, 0)
        if not self.after_last_write:
            self.link.connections[-1].peer_reset = True
        self.api.write_regs("D021", 0, 0)
        if self.after_last_write:
            self.link.connections[-1].peer_reset = True


class SetupOnALinkThatKeepsResettingTest(unittest.TestCase):
    """A link that answers every round trip but resets once per setup takes
    the bounded lossy retry, not the unbounded outage wait: the answered
    IDENTIFY after a redial that may have dropped commands is an answer."""

    def _run(self, *, after_last_write: bool) -> _ResettingScene:
        from c64cast.app.playlist import SETUP_LOSSY_TRIES
        from c64cast.hw.api import Ultimate64API

        link = _Flaky(down_dials=0)
        for target, value in (
            ("c64cast.hw.socket_dma.socket.create_connection", link.dial),
            ("c64cast.app.playlist.SETUP_RETRY_S", 0.0),
        ):
            p = patch(target, side_effect=value) if callable(value) else patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
            api = Ultimate64API("http://example.invalid")
        self.addCleanup(api.close)
        scene = _ResettingScene(api, link, after_last_write=after_last_write)
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)

        def stop_a_runaway() -> None:
            if scene.setup_count > SETUP_LOSSY_TRIES:
                pl.stop_event.set()

        scene.on_setup = stop_a_runaway
        with (
            self.assertLogs("c64cast.app.playlist", level="WARNING") as logs,
            self.assertLogs("c64cast.hw", level="DEBUG"),
        ):
            pl.safe_setup(scene)
        self.assertEqual(scene.setup_count, SETUP_LOSSY_TRIES)
        self.assertIn("keeping it as set up", logs.output[-1])
        return scene

    def test_a_reset_between_the_setup_writes_is_retried_a_bounded_number_of_times(self):
        self._run(after_last_write=False)

    def test_a_reset_after_the_last_setup_write_is_retried_a_bounded_number_of_times(self):
        self._run(after_last_write=True)


class _AudioScene(_LossySetupScene):
    def competes_for_audio_lock(self) -> bool:
        return True


class SetupOutageReleasesTheEnsembleAudioSlotTest(unittest.TestCase):
    """A system waiting out an outage in a scene's setup lets the rest of
    the ensemble have the audio slot, and takes it back when its link does."""

    def setUp(self) -> None:
        p = patch("c64cast.app.playlist.SETUP_RETRY_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def _ensemble_playlist(self, api: _OutageApi, scene: FakeScene) -> tuple[Playlist, Any]:
        from _fakes import fake_system_stack

        from c64cast.app.ensemble import Ensemble

        stop_event = threading.Event()
        pl = Playlist(
            [scene],
            api,
            name="sys",
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop_event,
            interstitial_factory=_transition_factory()[0],
        )
        pl.link_outage = RenderLinkOutage(pl.log, lambda: 0.0)
        ens = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=stop_event
        )
        pl.ensemble = ens
        self.assertTrue(pl.ensemble_coord.wait_for_audio_claim(scene))
        return pl, ens

    def test_the_slot_is_free_during_the_wait_and_held_again_after(self):
        api = _OutageApi(down_probes=3)
        scene = _AudioScene(api, lossy_setups=1)
        pl, ens = self._ensemble_playlist(api, scene)
        holders: list[str | None] = []
        api.on_probe = lambda: holders.append(ens.audio_holder)
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl.safe_setup(scene)
        # The first probe is the one that finds the link down; the wait follows.
        self.assertEqual(holders, ["sys"] + [None] * 3, "the slot stayed held through the wait")
        self.assertEqual(ens.audio_holder, "sys")
        self.assertTrue(scene.__dict__.get("_audio_lock_held"))
        self.assertEqual(scene.setup_count, 2)

    def test_an_interstitial_waiting_on_the_link_frees_the_slot_claimed_for_the_next_scene(self):
        api = _OutageApi(down_probes=3)
        upcoming = _AudioScene(api, lossy_setups=0)
        pl, ens = self._ensemble_playlist(api, upcoming)
        card = _LossySetupScene(api, lossy_setups=1)
        holders: list[str | None] = []
        api.on_probe = lambda: holders.append(ens.audio_holder)
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl.safe_setup(card)
        self.assertEqual(holders, ["sys"] + [None] * 3, "the slot stayed held through the wait")
        self.assertEqual(ens.audio_holder, "sys")
        self.assertTrue(upcoming.__dict__.get("_audio_lock_held"))
        self.assertEqual(card.setup_count, 2)

    def test_the_half_set_up_scene_is_torn_down_before_it_waits_for_the_slot(self):
        api = _OutageApi(down_probes=2)
        scene = _AudioScene(api, lossy_setups=1)
        pl, ens = self._ensemble_playlist(api, scene)

        def other_takes_it() -> None:
            if api.probes == 2:
                self.assertTrue(ens.try_claim_audio("other"))

        claim = ens.try_claim_audio
        teardowns_at_refusal: list[int] = []

        def other_lets_go_once_refused(name: str) -> bool:
            won = claim(name)
            if not won:
                teardowns_at_refusal.append(scene.teardown_count)
                ens.release_audio("other")
            return won

        api.on_probe = other_takes_it
        ens.try_claim_audio = other_lets_go_once_refused
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl.safe_setup(scene)
        self.assertEqual(
            teardowns_at_refusal, [1], "the scene stayed set up while another system had the slot"
        )
        self.assertEqual(ens.audio_holder, "sys")
        self.assertEqual(scene.setup_count, 2)

    def test_a_stop_while_reclaiming_a_slot_taken_meanwhile_ends_the_setup(self):
        api = _OutageApi(down_probes=2)
        scene = _AudioScene(api, lossy_setups=1)
        pl, ens = self._ensemble_playlist(api, scene)

        def other_takes_it() -> None:
            if api.probes == 2:
                self.assertTrue(ens.try_claim_audio("other"))

        claim = ens.try_claim_audio

        def stop_once_refused(name: str) -> bool:
            won = claim(name)
            if not won:
                pl.stop_event.set()
            return won

        api.on_probe = other_takes_it
        ens.try_claim_audio = stop_once_refused
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.safe_setup(scene)
        self.assertTrue(pl.stop_event.is_set())
        self.assertEqual(ens.audio_holder, "other")
        self.assertFalse(scene.__dict__.get("_audio_lock_held"))
        self.assertEqual(scene.setup_count, 1, "the scene set up again without the slot")
        self.assertTrue(any("waiting" in line for line in logs.output), logs.output)


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
