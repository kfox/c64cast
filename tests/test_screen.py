"""Tests for the console's screen feed: what it refuses, and when the machine's
video stream is up.

The lifetime is the whole point of this module and the thing worth pinning
down. The stream is a couple of megabytes a second, so it must be up exactly
while somebody is watching — starting it twice, or leaving it up after the last
watcher goes, are both failures nobody sees locally and everybody's network
sees. `_FakeApi` counts starts and stops so those are assertions rather than
hopes.

Not covered here: the HTTP routes (tests/test_web_api.py) and the wire format
(tests/test_vic_stream.py)."""

from __future__ import annotations

import time
import unittest
from unittest import mock

import numpy as np

from c64cast.control import screen as screen_mod
from c64cast.control.screen import ScreenFeed, ScreenUnavailable
from c64cast.hw.vic_stream import VicFrame


class _FakeProfile:
    def __init__(self, *, streams: bool, name: str = "Ultimate 64") -> None:
        self.supports_video_stream = streams
        self.name = name


class _FakeReceiver:
    def __init__(self, owner: _FakeApi) -> None:
        self.owner = owner
        self.frame: VicFrame | None = None

    def start(self) -> None:
        self.owner.starts += 1

    def stop(self) -> None:
        self.owner.stops += 1

    def latest(self) -> VicFrame | None:
        return self.frame


class _FakeApi:
    def __init__(self, *, streams: bool = True, opens: bool = True) -> None:
        self.profile = _FakeProfile(streams=streams)
        self.starts = 0
        self.stops = 0
        self.opens = opens
        self.receiver: _FakeReceiver | None = None

    def open_video_stream(self) -> _FakeReceiver:
        if not self.opens:
            raise RuntimeError("the machine said no")
        self.receiver = _FakeReceiver(self)
        return self.receiver


def _frame(number: int = 1, *, fill: int = 6) -> VicFrame:
    return VicFrame(np.full((8, 16), fill, dtype=np.uint8), number, 0.0)


class ResolveTest(unittest.TestCase):
    def test_nothing_running_says_so(self):
        feed = ScreenFeed(dict)
        with self.assertRaises(ScreenUnavailable) as caught:
            feed.resolve(None)
        self.assertIn("nothing is running", str(caught.exception))

    def test_one_system_needs_no_name(self):
        feed = ScreenFeed(lambda: {"c64cast": _FakeApi()})
        self.assertEqual(feed.resolve(None), "c64cast")

    def test_an_unknown_name_lists_the_ones_there_are(self):
        feed = ScreenFeed(lambda: {"left": _FakeApi(), "right": _FakeApi()})
        with self.assertRaises(ScreenUnavailable) as caught:
            feed.resolve("middle")
        self.assertIn("left", str(caught.exception))

    def test_a_machine_without_a_vic_of_its_own_is_refused_by_name(self):
        api = _FakeApi(streams=False)
        api.profile.name = "Ultimate II+"
        feed = ScreenFeed(lambda: {"c64cast": api})
        with self.assertRaises(ScreenUnavailable) as caught:
            feed.resolve(None)
        self.assertIn("Ultimate II+", str(caught.exception))

    def test_available_answers_without_starting_anything(self):
        api = _FakeApi()
        feed = ScreenFeed(lambda: {"c64cast": api})
        self.assertEqual(feed.available(), {"c64cast": True})
        self.assertEqual(api.starts, 0)


class LifetimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _FakeApi()
        self.feed = ScreenFeed(lambda: {"c64cast": self.api})

    def test_the_stream_comes_up_for_a_watcher_and_stays_up_for_a_second(self):
        with self.feed.watching("c64cast"):
            self.assertEqual(self.api.starts, 1)
            with self.feed.watching("c64cast"):
                # One machine, one stream: a second viewer is another reader of
                # the same frames, not another 2.6 MB/s.
                self.assertEqual(self.api.starts, 1)
        self.assertEqual(self.api.stops, 0)  # lingering, not gone

    def test_the_linger_ends_when_it_expires(self):
        with self.feed.watching("c64cast"):
            pass
        self.feed.sweep()
        self.assertEqual(self.api.stops, 0)  # too soon: a reload would pay for this
        self.feed._live["c64cast"].idle_since -= screen_mod.LINGER_S + 1
        self.feed.sweep()
        self.assertEqual(self.api.stops, 1)

    def test_a_watcher_arriving_during_the_linger_reuses_the_stream(self):
        with self.feed.watching("c64cast"):
            pass
        with self.feed.watching("c64cast"):
            self.assertEqual(self.api.starts, 1)
        self.assertEqual(self.api.stops, 0)

    def test_an_exception_in_the_body_still_releases(self):
        with self.assertRaises(ZeroDivisionError), self.feed.watching("c64cast"):
            raise ZeroDivisionError
        self.assertEqual(self.feed._live["c64cast"].watchers, 0)

    def test_close_stops_everything(self):
        with self.feed.watching("c64cast"):
            self.feed.close()
        self.assertEqual(self.api.stops, 1)

    def test_the_stream_ends_on_its_own_with_nothing_else_ticking(self):
        """The leak this feature shipped with for an afternoon, found on
        hardware: the sweep was driven by the state feed's push loop, and
        `/perf` (or a bare `<img>`) opens no WebSocket — so nothing ticked,
        nothing swept, and the machine went on sending 2.6 MB/s after the tab
        closed. Nothing in this test calls `sweep`."""
        with (
            mock.patch.object(screen_mod, "LINGER_S", 0.05),
            mock.patch.object(screen_mod, "_SWEEP_EVERY_S", 0.02),
        ):
            with self.feed.watching("c64cast"):
                self.assertEqual(self.api.starts, 1)
            deadline = time.monotonic() + 3.0
            while self.api.stops == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(self.api.stops, 1)
        # And the sweeper ends with the last receiver rather than idling on.
        deadline = time.monotonic() + 2.0
        while self.feed._sweeper is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(self.feed._sweeper)

    def test_no_thread_exists_until_something_is_watched(self):
        self.assertIsNone(self.feed._sweeper)
        self.feed.available()
        self.assertIsNone(self.feed._sweeper)

    def test_a_receiver_does_not_outlive_the_show_it_belongs_to(self):
        # A watcher still holding the stream open would otherwise keep a
        # receiver alive against a backend that is gone, re-arming a watchdog
        # over a link that no longer exists.
        running: dict[str, _FakeApi] = {"c64cast": self.api}
        feed = ScreenFeed(lambda: dict(running))
        with feed.watching("c64cast"):
            self.assertEqual(self.api.starts, 1)
            running.clear()
            feed.sweep()
            self.assertEqual(self.api.stops, 1)
        self.assertEqual(feed._live, {})

    def test_a_machine_that_refuses_to_open_leaves_nothing_behind(self):
        api = _FakeApi(opens=False)
        feed = ScreenFeed(lambda: {"c64cast": api})
        with self.assertRaises(ScreenUnavailable), feed.watching("c64cast"):
            pass  # pragma: no cover - acquire raises before the body
        self.assertEqual(feed._live, {})


class StopQuietlyTest(unittest.TestCase):
    def test_a_failure_to_stop_is_logged_and_not_raised(self):
        # Called from a sweep and from teardown, where there is nothing left to
        # tell: raising would take the sweeper thread down with it and leak
        # every other receiver.
        class _Stuck:
            def stop(self) -> None:
                raise OSError("the link went away")

        with self.assertLogs("c64cast.control.screen", level="ERROR"):
            screen_mod._stop_quietly("c64cast", _Stuck())

    def test_the_system_name_cannot_forge_a_log_line(self):
        """The name arrives from a `?system=` query parameter and the log drawer
        streams to a browser, so a newline in it would render as a log line of
        its own. `repr` cannot emit one — this pins the `%r` that makes that
        true (CodeQL alert 8 on #300)."""

        class _Stuck:
            def stop(self) -> None:
                raise OSError("the link went away")

        with self.assertLogs("c64cast.control.screen", level="ERROR") as caught:
            screen_mod._stop_quietly("c64cast\nERROR:c64cast: show cancelled", _Stuck())
        self.assertNotIn("\n", caught.records[0].getMessage())


class EncodeTest(unittest.TestCase):
    def test_a_frame_encodes_as_a_png_of_its_own_size(self):
        import cv2

        blob = screen_mod.encode_png(_frame())
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None
        self.assertEqual(decoded.shape, (8, 16, 3))

    def test_the_indices_are_mapped_through_the_palette(self):
        import cv2

        from c64cast.video.palette import C64_PALETTE_BGR

        blob = screen_mod.encode_png(_frame(fill=2))  # red
        decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None
        self.assertEqual(list(decoded[0, 0]), [int(c) for c in C64_PALETTE_BGR[2]])

    def test_png_rather_than_jpeg_is_the_smaller_one_on_this_content(self):
        # The reason for the choice, asserted rather than left in a comment:
        # flat 16-colour art with hard edges is PNG's best case and a DCT's
        # worst, so the usual "JPEG for video" advice inverts here.
        import cv2

        from c64cast.video.palette import C64_PALETTE_BGR

        # A full PAL frame of 8x8 cells in alternating colours — the hard edges
        # every character cell has, at the size a real one is.
        cells = np.indices((272 // 8, 384 // 8)).sum(axis=0) % 16
        indices = np.kron(cells, np.ones((8, 8), dtype=np.uint8)).astype(np.uint8)
        frame = VicFrame(indices, 1, 0.0)

        png = screen_mod.encode_png(frame)
        bgr = np.asarray(C64_PALETTE_BGR, dtype=np.uint8)[indices]
        ok, jpeg = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        self.assertLess(len(png), len(jpeg))


class MultipartTest(unittest.TestCase):
    def _parts(self, frames: list[VicFrame | None], count: int) -> list[bytes]:
        it = iter(frames)
        gen = screen_mod.multipart_frames(lambda: next(it, None), fps=1000.0)
        try:
            return [next(gen) for _ in range(count)]
        finally:
            gen.close()

    def test_a_part_carries_its_own_length_and_type(self):
        part = self._parts([_frame(1)], 1)[0]
        self.assertIn(b"Content-Type: image/png", part)
        head, body = part.split(b"\r\n\r\n", 1)
        length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
        self.assertEqual(len(body), length + 2)  # the trailing CRLF

    def test_an_unchanged_frame_is_not_re_sent(self):
        # A still C64 screen is the common case for a console left open, and
        # re-encoding it would be bytes for no picture.
        parts = self._parts([_frame(1), _frame(1), _frame(1), _frame(2)], 2)
        self.assertEqual(len(parts), 2)

    def test_closing_the_generator_is_what_ends_it(self):
        gen = screen_mod.multipart_frames(lambda: _frame(1), fps=1000.0)
        next(gen)
        gen.close()
        with self.assertRaises(StopIteration):
            next(gen)


if __name__ == "__main__":
    unittest.main()
