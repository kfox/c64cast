"""Tests for cli._run_playlists threading + teardown_stack ordering.

The cli module's per-stack lifecycle is exercised here with mocked stacks
so we don't have to bring up real APIs or playlists. End-to-end coverage
of real hardware lives outside the unittest suite (manual verification
against the U64 — see plan §4.2).

SystemStack carries typed fields (Ultimate64API, Playlist, ...) — we
stuff MagicMocks into them, so silence pyright's attribute-access
complaints file-wide rather than spraying ignores on every assertion."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from c64cast.cli import _run_playlists, teardown_stack
from c64cast.ensemble import SystemStack


def _fake_stack(name: str) -> SystemStack:
    return SystemStack(
        name=name,
        cfg=MagicMock(name=f"cfg-{name}"),
        api=MagicMock(name=f"api-{name}"),
        audio=None,
        source=None,
        playlist=MagicMock(name=f"playlist-{name}"),
        key_poller=MagicMock(name=f"key_poller-{name}"),
        framebuffer=None,
        preview_window=None,
        recorder=None,
    )


class RunPlaylistsTest(unittest.TestCase):
    def test_starts_one_thread_per_stack_and_joins(self):
        stop_event = threading.Event()
        stacks = [_fake_stack("a"), _fake_stack("b")]
        # playlist.run() returns immediately (no infinite loop here);
        # each thread exits and join() completes.
        for st in stacks:
            st.playlist.run.return_value = None
        _run_playlists(stacks, stop_event)
        for st in stacks:
            st.playlist.run.assert_called_once()
        # Sanity: no thread is left dangling.
        for t in threading.enumerate():
            self.assertFalse(t.name.startswith("playlist-"), f"playlist thread leaked: {t.name}")

    def test_stop_event_unblocks_blocking_playlists(self):
        # Playlists that block until stop_event is set should also join
        # cleanly when the event fires from outside.
        stop_event = threading.Event()
        stacks = [_fake_stack("a"), _fake_stack("b")]
        for st in stacks:
            st.playlist.run.side_effect = lambda: stop_event.wait()
        # Kick stop_event after a short delay so the main "join" loop
        # has a chance to enter join() on the first thread before exit.
        timer = threading.Timer(0.05, stop_event.set)
        timer.start()
        try:
            _run_playlists(stacks, stop_event)
        finally:
            timer.cancel()
        self.assertTrue(stop_event.is_set())
        for st in stacks:
            st.playlist.run.assert_called_once()


class TeardownStackOrderTest(unittest.TestCase):
    def _record_order(self) -> tuple[SystemStack, list[str]]:
        order: list[str] = []
        st = _fake_stack("only")
        st.preview_window = MagicMock()
        st.preview_window.stop.side_effect = lambda: order.append("preview")
        st.recorder = MagicMock()
        st.recorder.stop.side_effect = lambda: order.append("recorder")
        st.audio = MagicMock()
        st.audio.close.side_effect = lambda: order.append("audio")
        st.source = MagicMock()
        st.source.release.side_effect = lambda: order.append("source")
        st.api.reset.side_effect = lambda: order.append("reset")
        st.api.close.side_effect = lambda: order.append("api_close")
        return st, order

    def test_teardown_order(self):
        # Preview/recording first (avoid rendering after API close);
        # audio before reset (NMI timer can't fire into a cleared buffer);
        # api.reset → api.close; camera release last.
        st, order = self._record_order()
        teardown_stack(st)
        self.assertEqual(order, ["preview", "recorder", "audio", "reset", "api_close", "source"])

    def test_one_failure_doesnt_strand_remaining_steps(self):
        st, order = self._record_order()
        st.audio.close.side_effect = lambda: (_ for _ in ()).throw(RuntimeError("audio gone weird"))
        with self.assertLogs("c64cast", level="ERROR"):
            teardown_stack(st)
        # The failing step is skipped; everything after it still runs.
        self.assertEqual(order, ["preview", "recorder", "reset", "api_close", "source"])

    def test_missing_optional_resources_skipped(self):
        # framebuffer / preview_window / recorder are all None by default.
        st = _fake_stack("only")
        teardown_stack(st)
        st.api.reset.assert_called_once()
        st.api.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
