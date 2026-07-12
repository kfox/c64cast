"""Offline state-machine tests for c64cast.playlist.

These exercise Playlist with stub Scene + Api objects -- no U64 hardware,
no webcam, no audio device, no real network. The runtime env still needs
numpy/cv2/requests installed because importing the playlist module loads
them transitively (via .api and .scenes); but no actual call into those
libraries happens in these tests.

Run:    python -m unittest discover tests
   or:  python -m unittest tests.test_playlist
"""

# FakeScene + FakeApi are intentional duck-typed stubs of Scene/Ultimate64API;
# silence pyright's argument-type / attribute-access complaints across the file
# rather than spraying per-call ignores on every Playlist(...) construction.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import threading
import time
import unittest

from c64cast.playlist import Playlist

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeScene:
    """Mirrors the bits of Scene that Playlist relies on."""

    def __init__(
        self,
        name,
        frames_until_done=3,
        raise_on_frame=None,
        raise_on_setup=False,
        raise_on_teardown=False,
        prepare_renames_to=None,
    ):
        self.name = name
        self.is_done = False
        self.duration_s = 30.0
        self.setup_count = 0
        self.teardown_count = 0
        self.frame_count = 0
        self.frames_until_done = frames_until_done
        self.raise_on_frame = raise_on_frame
        self.raise_on_setup = raise_on_setup
        self.raise_on_teardown = raise_on_teardown
        # Mirrors a randomized scene that picks its file in prepare_next():
        # when set, prepare_next() renames the scene so the interstitial
        # built right after reflects the pick.
        self.prepare_renames_to = prepare_renames_to
        self.prepare_next_count = 0

    def prepare_next(self):
        self.prepare_next_count += 1
        if self.prepare_renames_to is not None:
            self.name = self.prepare_renames_to

    def setup(self):
        if self.raise_on_setup:
            raise RuntimeError(f"setup blew up in {self.name}")
        self.setup_count += 1
        self.is_done = False
        self.frame_count = 0

    def process_frame(self, current_time):
        self.frame_count += 1
        if self.raise_on_frame is not None and self.frame_count == self.raise_on_frame:
            raise RuntimeError(f"boom at frame {self.frame_count}")
        return self.frame_count < self.frames_until_done

    def teardown(self):
        if self.raise_on_teardown:
            raise RuntimeError(f"teardown blew up in {self.name}")
        self.teardown_count += 1


class FakeApi:
    """Playlist only uses .stats and .format_write_latency() (for the heartbeat)."""

    def __init__(self):
        self.stats = {
            "writes": 0,
            "skipped": 0,
            "errors": 0,
            "bytes": 0,
        }

    def format_write_latency(self):
        return None


def _transition_factory():
    """Return (factory, counter) — a Playlist interstitial_factory that
    yields a fast 1-frame FakeScene and tallies calls."""
    counter = {"n": 0}

    def factory(name):
        counter["n"] += 1
        return FakeScene(f"trans:{name}", frames_until_done=1)

    return factory, counter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class PlaylistTest(unittest.TestCase):
    def _run_briefly(
        self, scenes, stop_after=0.2, target_fps=10000.0, heartbeat_interval=0.0, loop=True
    ):
        api = FakeApi()
        stop_event = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            scenes,
            api,
            target_fps=target_fps,
            heartbeat_interval=heartbeat_interval,
            stop_event=stop_event,
            interstitial_factory=factory,
            loop=loop,
        )
        threading.Timer(stop_after, stop_event.set).start()
        pl.run()
        return counter

    # --- core state machine -----------------------------------------------

    def test_two_scene_loops_with_transitions(self):
        # Two scenes cycle A → interstitial → B → interstitial → A …
        # Single-scene mode (1 scene only) is exercised separately below;
        # this test keeps the multi-scene transition path covered.
        a = FakeScene("A", frames_until_done=2)
        b = FakeScene("B", frames_until_done=2)
        counter = self._run_briefly([a, b], stop_after=0.3)
        self.assertGreater(a.setup_count, 1, "scene A should re-setup on each playlist cycle")
        self.assertGreater(b.setup_count, 1, "scene B should re-setup on each playlist cycle")
        self.assertGreater(counter["n"], 2, "transitions should fire between cycles")
        # Every setup should be matched by a teardown (except possibly the
        # very last one, which the finally block in run() handles).
        self.assertGreaterEqual(a.teardown_count, a.setup_count - 1)
        self.assertGreaterEqual(b.teardown_count, b.setup_count - 1)

    def test_prepare_next_runs_before_interstitial(self):
        # A randomized scene renames itself in prepare_next(); the
        # interstitial that announces it must capture the renamed value,
        # proving prepare_next ran before the factory read .name.
        captured = []

        def factory(name):
            captured.append(name)
            return FakeScene(f"trans:{name}", frames_until_done=1)

        a = FakeScene("A", frames_until_done=1)
        b = FakeScene("B-spec", frames_until_done=1, prepare_renames_to="B-picked")
        api = FakeApi()
        stop_event = threading.Event()
        pl = Playlist(
            [a, b],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop_event,
            interstitial_factory=factory,
            loop=True,
        )
        threading.Timer(0.3, stop_event.set).start()
        pl.run()
        self.assertGreater(
            b.prepare_next_count, 0, "prepare_next must be called on the upcoming scene"
        )
        self.assertIn(
            "B-picked",
            captured,
            "interstitial must announce the prepared name, not the pre-prepare spec",
        )
        self.assertNotIn("B-spec", captured, "the stale pre-prepare name must never reach the card")

    # --- single-scene mode ------------------------------------------------

    def test_single_scene_skips_interstitial(self):
        # 1 scene → Playlist.single_scene = True → interstitial_factory
        # must never be called.
        s = FakeScene("A", frames_until_done=2)
        counter = self._run_briefly([s], stop_after=0.3)
        self.assertEqual(counter["n"], 0, "interstitial factory must not run in single-scene mode")
        self.assertGreater(s.setup_count, 0, "the single scene must still run")

    def test_single_scene_loops_indefinitely(self):
        # The scene flips is_done after 2 frames; single-scene mode must
        # re-setup it via teardown+setup so it runs again, not just stop.
        s = FakeScene("A", frames_until_done=2)
        self._run_briefly([s], stop_after=0.3)
        self.assertGreater(s.setup_count, 1, "scene should re-setup on each loop iteration")
        # Teardown happens on each loop boundary (and once more in finally).
        self.assertGreater(s.teardown_count, 1, "scene should tear down on each loop iteration")

    def test_single_scene_ignores_skip_event(self):
        # Firing skip_event in single-scene mode must NOT cause rapid-fire
        # teardown churn and must NOT invoke the interstitial factory.
        s = FakeScene("A", frames_until_done=10_000_000)
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_skips():
            for _ in range(5):
                time.sleep(0.02)
                pl.skip_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire_skips, daemon=True).start()
        pl.run()
        self.assertEqual(
            counter["n"], 0, "interstitial factory must not run on skip in single-scene mode"
        )
        # The scene was never marked done by skip → only the finally
        # teardown fires (count == 1). Tolerate >=1 in case of cleanup races.
        self.assertLessEqual(s.teardown_count, 1, "single-scene skip should not churn teardowns")
        self.assertFalse(
            pl.skip_event.is_set(), "skip event still gets cleared so it doesn't accumulate"
        )

    def test_multi_scene_rotation_starts_each(self):
        scenes = [FakeScene(c, frames_until_done=2) for c in "ABCD"]
        self._run_briefly(scenes, stop_after=0.5)
        for s in scenes:
            self.assertGreater(s.setup_count, 0, f"{s.name} never ran in rotation")

    # --- loop = False -----------------------------------------------------

    def test_single_scene_loop_false_exits_after_one_play(self):
        # loop=False + 1 scene: the scene plays once, marks is_done, and the
        # playlist sets stop_event + tears down instead of re-setting-up.
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        s = FakeScene("ONCE", frames_until_done=2)
        pl = Playlist(
            [s],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
            loop=False,
        )
        # Safety timer in case loop=False is broken and we'd otherwise spin
        # forever (matches the pre-fix video scene behavior).
        threading.Timer(2.0, stop.set).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        self.assertEqual(s.setup_count, 1, "scene should set up exactly once with loop=False")
        self.assertEqual(
            s.teardown_count,
            1,
            "scene should tear down exactly once (not in the "
            "run() finally — that path's already been taken)",
        )
        self.assertTrue(stop.is_set(), "loop=False end-of-playlist must set stop_event")
        self.assertEqual(counter["n"], 0, "single-scene mode never builds interstitials")
        self.assertTrue(
            any("finished and loop=False" in line for line in cap.output),
            f"expected loop=False exit log, got: {cap.output!r}",
        )

    def test_multi_scene_loop_false_exits_after_one_pass(self):
        # loop=False + N scenes: walk A → B → C → exit (no wrap back to A).
        scenes = [FakeScene(c, frames_until_done=1) for c in "ABC"]
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            scenes,
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
            loop=False,
        )
        threading.Timer(2.0, stop.set).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        for s in scenes:
            self.assertEqual(s.setup_count, 1, f"{s.name} should set up exactly once")
            self.assertEqual(s.teardown_count, 1, f"{s.name} should tear down exactly once")
        self.assertTrue(stop.is_set(), "loop=False end-of-playlist must set stop_event")
        self.assertTrue(
            any("playlist finished and loop=False" in line for line in cap.output),
            f"expected loop=False exit log, got: {cap.output!r}",
        )

    def test_loop_true_is_default(self):
        # Don't pass loop= — the default must preserve the previous looping
        # behavior, so this would otherwise duplicate the single-scene-loop
        # test. Quick sanity check that the constructor default is True.
        s = FakeScene("A", frames_until_done=2)
        pl = Playlist(
            [s],
            FakeApi(),
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=_transition_factory()[0],
        )
        self.assertTrue(pl.loop)

    # --- exception handling -----------------------------------------------

    def test_scene_exception_advances_playlist(self):
        bad = FakeScene("BAD", frames_until_done=100, raise_on_frame=2)
        good = FakeScene("GOOD", frames_until_done=2)
        # The scene crash is logged via log.exception — wrap in assertLogs
        # to both capture (silence) the traceback and verify the recovery path.
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            self._run_briefly([bad, good])
        self.assertTrue(
            any("scene 'BAD' raised; advancing" in line for line in cap.output),
            f"expected scene-crash log, got: {cap.output!r}",
        )
        self.assertGreater(bad.teardown_count, 0, "crashed scene must still be torn down")
        self.assertGreater(good.setup_count, 0, "playlist must advance to next scene after crash")

    def test_teardown_exception_does_not_kill_loop(self):
        bad_td = FakeScene("A", frames_until_done=1, raise_on_teardown=True)
        ok = FakeScene("B", frames_until_done=1)
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            self._run_briefly([bad_td, ok], stop_after=0.3)
        self.assertTrue(
            any("teardown of 'A' failed" in line for line in cap.output),
            f"expected teardown-failure log, got: {cap.output!r}",
        )
        self.assertGreater(bad_td.setup_count, 0)
        self.assertGreater(ok.setup_count, 0, "next scene must still start after teardown raises")

    def test_setup_exception_is_caught_in_advance(self):
        # If a scene's setup raises, the playlist logs it and the loop exits
        # cleanly (we don't try to recover from an unstartable scene).
        bad_setup = FakeScene("X", raise_on_setup=True)
        api = FakeApi()
        factory, _ = _transition_factory()
        pl = Playlist(
            [bad_setup],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=factory,
        )
        # Should not hang -- _advance catches and breaks.
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            pl.run()
        self.assertTrue(
            any("playlist advance failed; aborting" in line for line in cap.output),
            f"expected advance-failure log, got: {cap.output!r}",
        )

    # --- shutdown ---------------------------------------------------------

    def test_stop_event_halts_loop_promptly(self):
        s = FakeScene("A", frames_until_done=10_000_000)
        stop = threading.Event()
        api = FakeApi()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=60.0,  # real-ish frame time
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )
        threading.Timer(0.05, stop.set).start()
        t0 = time.time()
        pl.run()
        dt = time.time() - t0
        self.assertLess(dt, 1.0, f"stop_event should interrupt within ~50ms, took {dt:.2f}s")
        self.assertGreater(s.teardown_count, 0, "current scene must be torn down")

    def test_keyboard_interrupt_triggers_clean_teardown(self):
        s = FakeScene("A", frames_until_done=100)
        original_pf = s.process_frame
        n = {"v": 0}

        def pf(t):
            n["v"] += 1
            if n["v"] == 3:
                raise KeyboardInterrupt
            return original_pf(t)

        s.process_frame = pf

        api = FakeApi()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s], api, target_fps=10000.0, heartbeat_interval=0.0, interstitial_factory=factory
        )
        pl.run()  # should NOT re-raise
        self.assertGreater(
            s.teardown_count, 0, "KeyboardInterrupt must still teardown the current scene"
        )

    # --- heartbeat --------------------------------------------------------

    def test_heartbeat_emits_when_interval_set(self):
        s = FakeScene("A", frames_until_done=10_000)
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.05,
            stop_event=stop,
            interstitial_factory=factory,
        )
        threading.Timer(0.25, stop.set).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        self.assertTrue(
            any("writes=" in line for line in cap.output),
            f"no heartbeat lines in captured logs: {cap.output!r}",
        )

    def test_heartbeat_disabled_when_interval_zero(self):
        s = FakeScene("A", frames_until_done=10_000)
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        # Bump a stat so we'd see traffic if the heartbeat ran.
        api.stats["writes"] = 100
        pl = Playlist(
            [s],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )
        threading.Timer(0.2, stop.set).start()
        # No INFO logs should arrive from the playlist module.
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            # Inject one log so assertLogs doesn't error on "no logs captured".
            import logging

            logging.getLogger("c64cast.playlist").info("sentinel")
            pl.run()
        heartbeat_lines = [line for line in cap.output if "writes=" in line]
        self.assertEqual(heartbeat_lines, [], "no heartbeat lines expected when interval=0")

    # --- skip_event -------------------------------------------------------

    def test_skip_event_advances_current_scene(self):
        # A scene that would run for a long time. Setting skip_event after
        # the first frame should force is_done = True so the playlist
        # advances to an interstitial → the next scene.
        s = FakeScene("A", frames_until_done=10_000_000)
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            [s, FakeScene("B", frames_until_done=1)],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_skip():
            time.sleep(0.05)
            pl.skip_event.set()
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=fire_skip, daemon=True).start()
        pl.run()
        self.assertGreaterEqual(s.teardown_count, 1, "skip should tear down the current scene")
        self.assertGreater(counter["n"], 1, "skip should land on a new interstitial")

    # --- request_jump -------------------------------------------------------

    def test_request_jump_single_scene_is_noop(self):
        pl = Playlist(
            [FakeScene("A", frames_until_done=10_000_000)],
            FakeApi(),
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.request_jump(0)
        self.assertIsNone(pl._jump_target)
        self.assertFalse(pl.skip_event.is_set())

    def test_request_jump_out_of_range_raises(self):
        pl = Playlist(
            [FakeScene("A", frames_until_done=1), FakeScene("B", frames_until_done=1)],
            FakeApi(),
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        with self.assertRaises(ValueError):
            pl.request_jump(5)

    def test_request_jump_last_write_wins(self):
        pl = Playlist(
            [FakeScene(n, frames_until_done=1) for n in "ABC"],
            FakeApi(),
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
        )
        pl.request_jump(1)
        pl.request_jump(2)
        self.assertEqual(pl._jump_target, 2)

    def test_request_jump_lands_on_target_index(self):
        # A long-running scene at index 0; jump straight to index 2 (C) —
        # the walk-forward index+1 path must never land there on its own
        # within the short run window.
        scenes = [
            FakeScene("A", frames_until_done=10_000_000),
            FakeScene("B", frames_until_done=1),
            FakeScene("C", frames_until_done=10_000_000),
        ]
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            scenes,
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_jump():
            time.sleep(0.05)
            pl.request_jump(2)
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=fire_jump, daemon=True).start()
        pl.run()
        self.assertGreaterEqual(scenes[2].setup_count, 1, "jump should land on scene C")
        self.assertEqual(scenes[1].setup_count, 0, "jump must skip scene B entirely")

    def test_request_jump_skip_interstitial_bypasses_the_card(self):
        # Both scenes run "forever" (target_fps=200 over a 0.25s window
        # can't reach 10M frames) so the only scene transition possible in
        # this window is the jump itself — a stray natural completion of B
        # can't sneak in an unrelated interstitial and confound the count.
        scenes = [
            FakeScene("A", frames_until_done=10_000_000),
            FakeScene("B", frames_until_done=10_000_000),
        ]
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            scenes,
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        result: dict[str, int | None] = {"baseline": None}

        def fire_jump():
            # Wait past the playlist's own startup interstitial (every
            # playlist enters scene 0 via one "UP NEXT" card) so the
            # baseline below reflects a settled, running scene A —
            # otherwise a race against that first card would make the
            # baseline nondeterministic. Assertions run in the main thread
            # after pl.run() returns (an AssertionError raised here, in a
            # background thread, wouldn't fail the test).
            deadline = time.time() + 1.0
            while scenes[0].setup_count < 1 and time.time() < deadline:
                time.sleep(0.005)
            result["baseline"] = counter["n"]
            pl.request_jump(1, skip_interstitial=True)
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=fire_jump, daemon=True).start()
        pl.run()
        baseline = result["baseline"]
        assert baseline is not None, "startup interstitial never completed"
        self.assertEqual(counter["n"], baseline, "a cut jump must never build an interstitial")
        self.assertGreaterEqual(scenes[1].setup_count, 1)

    def test_request_jump_interstitial_transition_uses_the_card(self):
        scenes = [
            FakeScene("A", frames_until_done=10_000_000),
            FakeScene("B", frames_until_done=10_000_000),
        ]
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            scenes,
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        result: dict[str, int | None] = {"baseline": None}

        def fire_jump():
            deadline = time.time() + 1.0
            while scenes[0].setup_count < 1 and time.time() < deadline:
                time.sleep(0.005)
            result["baseline"] = counter["n"]
            pl.request_jump(1, skip_interstitial=False)
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=fire_jump, daemon=True).start()
        pl.run()
        baseline = result["baseline"]
        assert baseline is not None, "startup interstitial never completed"
        self.assertGreater(
            counter["n"], baseline, "an interstitial-routed jump must build the card"
        )
        self.assertGreaterEqual(scenes[1].setup_count, 1, "must still land on the target scene")

    def test_jump_to_audio_gated_scene_waits_on_the_same_gate_as_looping(self):
        # A jump target that competes for the ensemble audio lock must
        # block via _wait_for_audio_claim (the same gate single-scene
        # looping uses) rather than silently falling through to
        # _resolve_next_index's skip-past-gated-scenes behavior. Proven
        # here by pre-setting stop_event so the wait exits immediately
        # with current=None, instead of landing on the gated scene.
        from unittest.mock import MagicMock

        from c64cast.ensemble import Ensemble, SystemStack

        def _stack(name):
            return SystemStack(
                name=name,
                cfg=MagicMock(),
                api=MagicMock(),
                audio=None,
                source=None,
                playlist=MagicMock(),
                key_poller=MagicMock(),
                framebuffer=None,
                preview_window=None,
                recorder=None,
            )

        class AudioGatedScene(FakeScene):
            def competes_for_audio_lock(self):
                return True

        a = FakeScene("A", frames_until_done=1)
        b = AudioGatedScene("B", frames_until_done=1)
        stop_event = threading.Event()
        pl = Playlist(
            [a, b],
            FakeApi(),
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop_event,
            interstitial_factory=_transition_factory()[0],
        )
        ens = Ensemble(stacks=[_stack("sys"), _stack("other")], stop_event=stop_event)
        ens.try_claim_audio("other")  # slot held elsewhere
        pl.ensemble = ens
        pl.current = a
        a.is_done = True
        stop_event.set()  # wait loop inside _wait_for_audio_claim exits immediately
        pl.request_jump(1)
        pl._advance()
        self.assertIsNone(pl.current)

    # --- busy-deferral (overlay.is_busy() defers scene teardown) ---------

    def test_busy_overlay_defers_scene_teardown(self):
        # Scene finishes after 1 frame but a busy overlay should keep the
        # scene running until is_busy() flips False. Once it does, the
        # scene tears down on the next frame.
        scene = FakeScene("A", frames_until_done=1)

        class FakeOverlay:
            name = "fake"
            PAINTS_INTO_BUFFERS = False
            _disabled = False
            _busy = True
            setup_count = 0
            teardown_count = 0
            frame_count = 0

            def setup(self, api, scene):
                self.setup_count += 1

            def process_frame(self, api, scene, t):
                self.frame_count += 1
                # Flip busy off after a few extra frames so the test ends.
                if self.frame_count >= 5:
                    self._busy = False

            def teardown(self, api, scene):
                self.teardown_count += 1

            def is_busy(self):
                return self._busy

        overlay = FakeOverlay()
        scene.overlays = [overlay]
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )
        threading.Timer(0.3, stop.set).start()
        pl.run()
        # The overlay's process_frame should have been called more than
        # the scene's frames_until_done (1) — busy deferral let it keep
        # running for at least a few extra frames before teardown.
        self.assertGreater(overlay.frame_count, 1, "busy overlay should have deferred teardown")
        self.assertGreater(
            overlay.teardown_count, 0, "overlay should still tear down once unblocked"
        )

    def test_ctrl_skip_overrides_busy_overlay(self):
        # When an overlay reports busy AND skip_event is set, CTRL must
        # win — the scene tears down regardless of busy state.
        scene = FakeScene("A", frames_until_done=10_000_000)

        class StuckOverlay:
            name = "stuck"
            PAINTS_INTO_BUFFERS = False
            _disabled = False

            def setup(self, api, scene):
                pass

            def process_frame(self, api, scene, t):
                pass

            def teardown(self, api, scene):
                pass

            def is_busy(self):
                return True  # never finishes

        scene.overlays = [StuckOverlay()]
        api = FakeApi()
        stop = threading.Event()
        factory, counter = _transition_factory()
        pl = Playlist(
            [scene, FakeScene("B", frames_until_done=1)],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_skip():
            time.sleep(0.05)
            pl.skip_event.set()
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=fire_skip, daemon=True).start()
        pl.run()
        # CTRL should cut through the busy guard — scene torn down and
        # we landed on a new interstitial.
        self.assertGreaterEqual(scene.teardown_count, 1, "CTRL skip should override busy overlay")
        self.assertGreater(counter["n"], 1, "CTRL skip should advance to a new interstitial")

    # --- cycle_event ------------------------------------------------------

    def test_cycle_event_calls_display_mode_cycle_style(self):
        # SHIFT press → playlist calls display_mode.cycle_style(api) on
        # the current scene and logs the returned style name. Verify both.
        class FakeMode:
            name = "fake"
            calls = 0

            def cycle_style(self, api):
                self.calls += 1
                return "palette_mode=vivid"

        fake_mode = FakeMode()
        s = FakeScene("A", frames_until_done=10_000_000)
        s.display_mode = fake_mode
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_cycle():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire_cycle, daemon=True).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        self.assertEqual(
            fake_mode.calls, 1, "cycle_event should invoke display_mode.cycle_style once"
        )
        self.assertTrue(
            any("cycle:" in line and "palette_mode=vivid" in line for line in cap.output),
            f"expected cycle log line, got {cap.output!r}",
        )
        self.assertFalse(pl.cycle_event.is_set(), "cycle event must be cleared after use")

    def test_cycle_event_ignored_during_interstitial(self):
        # SHIFT during the interstitial transition is a no-op (cycling the
        # interstitial's bg mid-flight isn't a useful UX).
        class CycleCounter:
            calls = 0

            def cycle_style(self, api):
                self.calls += 1
                return "x"

        counter = CycleCounter()
        a = FakeScene("A", frames_until_done=1)
        a.display_mode = counter
        b = FakeScene("B", frames_until_done=1)
        b.display_mode = counter
        api = FakeApi()
        stop = threading.Event()

        # Custom factory: returns a long-lived interstitial so the cycle
        # firing during the transition has a chance to be dropped.
        def factory(name):
            return FakeScene(f"trans:{name}", frames_until_done=10_000_000)

        pl = Playlist(
            [a, b],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire_cycle():
            # Let the first interstitial start, then fire cycle while it's
            # still active, then stop.
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire_cycle, daemon=True).start()
        pl.run()
        self.assertEqual(counter.calls, 0, "cycle must not dispatch on an interstitial scene")

    def test_cycle_event_also_broadcasts_to_overlays(self):
        # SHIFT cycle should reach every attached overlay that opts in.
        class CycleMode:
            def cycle_style(self, api):
                return "mode_x"

        class CycleOverlay:
            name = "decorated"
            PAINTS_INTO_BUFFERS = False
            _disabled = False
            cycle_calls = 0

            def setup(self, api, scene):
                pass

            def process_frame(self, api, scene, t):
                pass

            def teardown(self, api, scene):
                pass

            def is_busy(self):
                return False

            def cycle_style(self, api, scene):
                self.cycle_calls += 1
                return "ovl_y"

        s = FakeScene("A", frames_until_done=10_000_000)
        s.display_mode = CycleMode()
        overlay = CycleOverlay()
        s.overlays = [overlay]
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        self.assertEqual(overlay.cycle_calls, 1, "cycle_event must dispatch to opt-in overlays")
        # Log should mention both the display and overlay labels.
        self.assertTrue(
            any("display=mode_x" in line and "decorated=ovl_y" in line for line in cap.output),
            f"expected combined cycle label, got {cap.output!r}",
        )

    def test_cycle_event_no_op_when_mode_returns_none(self):
        # Default DisplayMode.cycle_style returns None → playlist logs
        # at debug level (not info) and clears the event without crashing.
        class StaticMode:
            def cycle_style(self, api):
                return None

        s = FakeScene("A", frames_until_done=10_000_000)
        s.display_mode = StaticMode()
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        pl.run()  # must not raise
        self.assertFalse(pl.cycle_event.is_set())

    def test_cycle_event_catches_exception(self):
        # cycle_style raising must not crash the run loop.
        class BadMode:
            def cycle_style(self, api):
                raise RuntimeError("kaboom")

        s = FakeScene("A", frames_until_done=10_000_000)
        s.display_mode = BadMode()
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            pl.run()
        self.assertTrue(
            any("cycle_style failed" in line for line in cap.output),
            f"expected cycle_style failure log, got {cap.output!r}",
        )
        self.assertGreater(s.teardown_count, 0, "scene still tears down cleanly after cycle error")

    def test_cycle_event_dispatches_to_scene_when_no_display_mode(self):
        # Scenes without a display_mode (waveform, midi) still participate
        # in SHIFT cycling via their own cycle_style(api) method.
        class SceneWithCycle(FakeScene):
            def __init__(self, name):
                super().__init__(name, frames_until_done=10_000_000)
                self.display_mode = None
                self.cycle_calls = 0

            def cycle_style(self, api):
                self.cycle_calls += 1
                return "song 2/5"

        s = SceneWithCycle("WAVE")
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl.run()
        self.assertEqual(s.cycle_calls, 1, "cycle_event must invoke scene.cycle_style once")
        self.assertTrue(
            any("scene=song 2/5" in line for line in cap.output),
            f"expected scene cycle log, got {cap.output!r}",
        )

    def test_cycle_event_scene_exception_does_not_crash_loop(self):
        # A scene-level cycle_style raising must be caught — same contract
        # as display_mode.cycle_style.
        class BadScene(FakeScene):
            def __init__(self, name):
                super().__init__(name, frames_until_done=10_000_000)
                self.display_mode = None

            def cycle_style(self, api):
                raise RuntimeError("scene cycle kaboom")

        s = BadScene("WAVE")
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )

        def fire():
            time.sleep(0.05)
            pl.cycle_event.set()
            time.sleep(0.1)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            pl.run()
        self.assertTrue(
            any("cycle_style failed on scene" in line for line in cap.output),
            f"expected scene cycle failure log, got {cap.output!r}",
        )

    def test_skip_event_is_cleared_after_use(self):
        # Skipping once shouldn't leak into permanently rapid-firing.
        s = FakeScene("A", frames_until_done=2)
        api = FakeApi()
        stop = threading.Event()
        factory, _ = _transition_factory()
        pl = Playlist(
            [s],
            api,
            target_fps=200.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=factory,
        )
        pl.skip_event.set()
        threading.Timer(0.1, stop.set).start()
        pl.run()
        # Event should have been auto-cleared by the run loop.
        self.assertFalse(pl.skip_event.is_set())


class _DisturbanceAudio:
    """Records note_playback_disturbance() calls (the only audio method the
    frame-drop path touches)."""

    def __init__(self):
        self.disturbances = 0

    def note_playback_disturbance(self):
        self.disturbances += 1


class FrameDropDisturbanceTest(unittest.TestCase):
    """A large deadline snap-forward signals the audio loop; a small one doesn't."""

    def _playlist(self, audio):
        return Playlist(
            [FakeScene("A", frames_until_done=10_000)],
            FakeApi(),
            target_fps=10000.0,  # frame_time = 1e-4 s
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=_transition_factory()[0],
            audio=audio,
        )

    def test_large_drop_signals_audio(self):
        audio = _DisturbanceAudio()
        pl = self._playlist(audio)
        # Deadline ~1 s in the past → drops ≫ _AUDIO_DISTURBANCE_DROP_S of frames.
        pl._run_one_frame(pl.scenes[0], time.time() - 1.0)
        self.assertEqual(audio.disturbances, 1)

    def test_small_drop_does_not_signal(self):
        audio = _DisturbanceAudio()
        pl = self._playlist(audio)
        # ~10 ms behind: a real (>2 frame) drop, but well under the 0.5 s bar.
        pl._run_one_frame(pl.scenes[0], time.time() - 0.01)
        self.assertEqual(audio.disturbances, 0)

    def test_no_audio_is_safe(self):
        pl = self._playlist(None)
        pl._run_one_frame(pl.scenes[0], time.time() - 1.0)  # must not raise


class SceneRecordingMetadataTest(unittest.TestCase):
    """_safe_setup logs one SCENE_CONFIG_JSON line per activation when a
    Config is attached, and stays silent without one."""

    def _playlist(self, config):
        return Playlist(
            [FakeScene("A", frames_until_done=3)],
            FakeApi(),
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=_transition_factory()[0],
            config=config,
        )

    def test_logs_once_per_setup_with_config(self):
        from c64cast.config import Config
        from c64cast.recording_metadata import SCENE_CONFIG_MARKER

        pl = self._playlist(Config())
        with self.assertLogs("c64cast.recording", level="INFO") as cap:
            pl._safe_setup(pl.scenes[0])
        self.assertEqual(len(cap.output), 1)
        self.assertIn(SCENE_CONFIG_MARKER, cap.output[0])

    def test_no_config_logs_nothing(self):
        pl = self._playlist(None)
        with self.assertNoLogs("c64cast.recording", level="INFO"):
            pl._safe_setup(pl.scenes[0])


class PlaylistUserDimTest(unittest.TestCase):
    """The persistent WLED brightness dim lives on the Playlist and is
    re-stamped onto each fresh scene's display mode at setup, so a dim set via
    the app survives scene auto-advance (mode instances are per-scene)."""

    class _Mode:
        def __init__(self, user_dim: float = 1.0):
            self.user_dim = user_dim

    def _playlist(self, scenes):
        return Playlist(
            scenes,
            FakeApi(),
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=_transition_factory()[0],
            fade_duration_s=0.0,
        )

    def test_default_user_dim_is_full_brightness(self):
        self.assertEqual(self._playlist([FakeScene("A")]).user_dim, 1.0)

    def test_safe_setup_stamps_dim_onto_each_fresh_mode(self):
        a, b = FakeScene("A"), FakeScene("B")
        a.display_mode = self._Mode()
        b.display_mode = self._Mode()
        pl = self._playlist([a, b])
        pl.user_dim = 0.4
        pl._safe_setup(a)
        pl._safe_setup(b)  # a later auto-advance re-applies the same dim
        self.assertEqual(a.display_mode.user_dim, 0.4)
        self.assertEqual(b.display_mode.user_dim, 0.4)

    def test_full_brightness_leaves_mode_untouched(self):
        # At the 1.0 default the stamp is skipped, so a mode dimmed for any
        # other reason isn't clobbered — only a real dim is pushed.
        a = FakeScene("A")
        a.display_mode = self._Mode(user_dim=0.7)
        pl = self._playlist([a])  # user_dim stays 1.0
        pl._safe_setup(a)
        self.assertEqual(a.display_mode.user_dim, 0.7)


if __name__ == "__main__":
    unittest.main()
