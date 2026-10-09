"""Tests for ensemble audio coordination.

Three layers:
  1. `Ensemble.try_claim_audio` / `release_audio` — the atomic primitive.
  2. `scene_factory.build_scene(..., is_ensemble=True)` — live scenes (webcam,
     blank) build with audio=None so they can't compete for the SID.
  3. `Playlist.ensemble_coord.resolve_next_index` + `_safe_teardown` — gating
     audio-bearing scene advancement and releasing the slot on teardown.

No real U64, no real audio device, no real webcam — every dependency is
faked.
"""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
import sys
import threading
import unittest
from typing import cast
from unittest.mock import MagicMock

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.app.ensemble import Ensemble
from c64cast.app.playlist import Playlist
from c64cast.scenes.scenes import BlankScene, Scene, VideoScene, WebcamScene

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI, fake_system_stack  # noqa: E402
from test_playlist import FakeApi, FakeScene  # noqa: E402


class EnsembleAudioLockTest(unittest.TestCase):
    def _ensemble(self, names):
        return Ensemble(stacks=[fake_system_stack(n) for n in names], stop_event=threading.Event())

    def test_first_claim_wins(self):
        ens = self._ensemble(["a", "b"])
        self.assertTrue(ens.try_claim_audio("a"))
        self.assertEqual(ens.audio_holder, "a")

    def test_second_claim_by_other_loses(self):
        ens = self._ensemble(["a", "b"])
        ens.try_claim_audio("a")
        self.assertFalse(ens.try_claim_audio("b"))
        self.assertEqual(ens.audio_holder, "a")

    def test_reclaim_by_same_holder_succeeds(self):
        # A repeat setup() (single-scene loop, follower restore) must not
        # deadlock on a slot we already own.
        ens = self._ensemble(["a"])
        ens.try_claim_audio("a")
        self.assertTrue(ens.try_claim_audio("a"))
        self.assertEqual(ens.audio_holder, "a")

    def test_release_by_holder_frees_slot(self):
        ens = self._ensemble(["a", "b"])
        ens.try_claim_audio("a")
        ens.release_audio("a")
        self.assertIsNone(ens.audio_holder)
        self.assertTrue(ens.try_claim_audio("b"))

    def test_release_by_non_holder_is_noop(self):
        # Teardown paths must tolerate a stale release: never raise, never
        # clobber the real holder.
        ens = self._ensemble(["a", "b"])
        ens.try_claim_audio("a")
        ens.release_audio("b")  # doesn't hold the slot
        self.assertEqual(ens.audio_holder, "a")

    def test_release_when_unheld_is_noop(self):
        ens = self._ensemble(["a"])
        ens.release_audio("a")  # no-op, no exception
        self.assertIsNone(ens.audio_holder)

    def test_concurrent_claims_only_one_wins(self):
        ens = self._ensemble(["a"] * 32)
        wins: list[bool] = []
        ready = threading.Barrier(32)

        def race(name):
            ready.wait()
            wins.append(ens.try_claim_audio(name))

        threads = [threading.Thread(target=race, args=(f"sys{i}",)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for w in wins if w), 1)


class EnsembleLiveSceneSuppressionTest(unittest.TestCase):
    def setUp(self):
        from c64cast.audio.audio import AudioStreamer
        from c64cast.hw.api import Ultimate64API
        from c64cast.video.video import WebcamSource

        self.api = cast(Ultimate64API, FakeAPI())
        self.audio_sentinel = cast(AudioStreamer, object())
        self.source = cast(WebcamSource, object())
        self.cfg = cfgmod.Config()

    def test_webcam_audio_suppressed_in_ensemble_mode(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(
            s, self.cfg, self.api, self.audio_sentinel, self.source, is_ensemble=True
        )
        self.assertIsNone(scene.audio, "live webcam scene must not hold audio in ensemble")

    def test_blank_audio_suppressed_in_ensemble_mode(self):
        s = cfgmod.SceneCfg(type="blank")
        scene = scene_factory.build_scene(
            s, self.cfg, self.api, self.audio_sentinel, None, is_ensemble=True
        )
        self.assertIsNone(scene.audio)

    def test_webcam_explicit_audio_true_is_logged_when_suppressed(self):
        # A user who typed `audio = true` on a live scene has to be told it
        # was overridden.
        s = cfgmod.SceneCfg(type="webcam", display="petscii", audio=True)
        with self.assertLogs("c64cast.app.scene_factory", level="INFO") as cap:
            scene = scene_factory.build_scene(
                s, self.cfg, self.api, self.audio_sentinel, self.source, is_ensemble=True
            )
        self.assertIsNone(scene.audio)
        self.assertTrue(any("audio suppressed in ensemble" in line for line in cap.output))

    def test_single_system_mode_unaffected(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIs(scene.audio, self.audio_sentinel)


class WantsAudioLockFlagTest(unittest.TestCase):
    """Class-level claim flags. Spot-check each audio-bearing scene class
    so a future rename / refactor doesn't silently drop the marker."""

    def test_base_scene_default_false(self):
        self.assertFalse(Scene.WANTS_AUDIO_LOCK)

    def test_webcam_scene_does_not_claim(self):
        self.assertFalse(WebcamScene.WANTS_AUDIO_LOCK)

    def test_blank_scene_does_not_claim(self):
        self.assertFalse(BlankScene.WANTS_AUDIO_LOCK)

    def test_video_scene_claims(self):
        self.assertTrue(VideoScene.WANTS_AUDIO_LOCK)

    def test_waveform_scene_claims(self):
        # Local import: waveform pulls in songlengths, heavier than the
        # live scenes.
        from c64cast.sid.waveform import WaveformScene

        self.assertTrue(WaveformScene.WANTS_AUDIO_LOCK)

    def test_midi_scene_claims(self):
        from c64cast.sid.midi_scene import MidiScene

        self.assertTrue(MidiScene.WANTS_AUDIO_LOCK)


class CompetesForAudioLockTest(unittest.TestCase):
    """The class flag declares the capability; the instance predicate
    decides whether THIS scene actually contends. A muted video
    (audio=None) opts out; SID-driving scenes always compete."""

    def test_base_scene_follows_flag(self):
        scene = Scene.__new__(Scene)
        scene.audio = None
        scene.WANTS_AUDIO_LOCK = False
        self.assertFalse(scene.competes_for_audio_lock())
        scene.WANTS_AUDIO_LOCK = True
        self.assertTrue(scene.competes_for_audio_lock())

    def test_video_with_audio_competes(self):
        comm = VideoScene.__new__(VideoScene)
        comm.audio = MagicMock(name="streamer")
        self.assertTrue(comm.competes_for_audio_lock())

    def test_muted_video_does_not_compete(self):
        comm = VideoScene.__new__(VideoScene)
        comm.audio = None
        self.assertFalse(comm.competes_for_audio_lock())

    def test_waveform_competes_even_without_streamer(self):
        # WaveformScene drives the SID directly, so it contends whether or
        # not an AudioStreamer was wired in.
        from c64cast.sid.waveform import WaveformScene

        wf = WaveformScene.__new__(WaveformScene)
        wf.audio = None
        self.assertTrue(wf.competes_for_audio_lock())

    def test_midi_competes_even_without_streamer(self):
        from c64cast.sid.midi_scene import MidiScene

        midi = MidiScene.__new__(MidiScene)
        midi.audio = None
        self.assertTrue(midi.competes_for_audio_lock())


class FakePlaylistScene:
    """Mirrors enough of Scene for Playlist to drive it. WANTS_AUDIO_LOCK
    is set per instance via the constructor so a single test can build
    mixed playlists. `audio` defaults to a truthy sentinel so an
    audio-bearing fake contends by default; pass `audio=None` to model a
    muted scene that should fall through like a non-audio scene."""

    def __init__(self, name, wants_audio=False, frames_until_done=1, audio="streamer"):
        self.name = name
        self.WANTS_AUDIO_LOCK = wants_audio
        self.audio = audio
        self.is_done = False
        self.duration_s = 30.0
        self.target_fps = None
        self.overlays: list = []
        self.display_mode = MagicMock()
        self.display_mode.default_target_fps = None
        self.setup_calls = 0
        self.teardown_calls = 0
        self.frame_count = 0
        self.frames_until_done = frames_until_done

    def competes_for_audio_lock(self):
        return self.WANTS_AUDIO_LOCK and self.audio is not None

    def setup(self):
        self.setup_calls += 1

    def teardown(self):
        self.teardown_calls += 1

    def process_frame(self, t):
        self.frame_count += 1
        return self.frame_count < self.frames_until_done


def _build_playlist(scenes, name="sys"):
    api = MagicMock()
    api.stats = {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}
    api.format_write_latency.return_value = None
    return Playlist(
        scenes=scenes,
        api=api,
        target_fps=60.0,
        heartbeat_interval=0.0,
        stop_event=threading.Event(),
        interstitial_factory=lambda nm: FakePlaylistScene(f"interstitial:{nm}"),
        key_poller=None,
        name=name,
    )


class ResolveNextIndexTest(unittest.TestCase):
    def test_no_ensemble_returns_self_index(self):
        # Single-system runs never instantiate an Ensemble, so the helper
        # must not try to touch one.
        pl = _build_playlist([FakePlaylistScene("a"), FakePlaylistScene("b")])
        pl.index = 1
        self.assertEqual(pl.ensemble_coord.resolve_next_index(), 1)

    def test_non_audio_scene_passes_through(self):
        pl = _build_playlist(
            [FakePlaylistScene("a", wants_audio=False), FakePlaylistScene("b", wants_audio=True)]
        )
        pl.ensemble = Ensemble(stacks=[fake_system_stack("sys")], stop_event=pl.stop_event)
        self.assertEqual(pl.ensemble_coord.resolve_next_index(), 0)

    def test_audio_scene_claims_lock_when_free(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[fake_system_stack("sys")], stop_event=pl.stop_event)
        self.assertEqual(pl.ensemble_coord.resolve_next_index(), 0)
        self.assertEqual(pl.ensemble.audio_holder, "sys")
        self.assertTrue(scene.__dict__["_audio_lock_held"])

    def test_audio_scene_skipped_when_lock_held_elsewhere(self):
        comm = FakePlaylistScene("video", wants_audio=True)
        live = FakePlaylistScene("live", wants_audio=False)
        pl = _build_playlist([comm, live])
        pl.ensemble = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        with self.assertLogs("c64cast.app.playlist", level="INFO") as cap:
            idx = pl.ensemble_coord.resolve_next_index()
        self.assertEqual(idx, 1)
        self.assertTrue(any("skipping audio-bearing" in line for line in cap.output))

    def test_muted_audio_scene_passes_through_when_lock_held(self):
        # An audio-capable scene with audio disabled does not contend: it
        # is returned directly and never claims the lock.
        muted = FakePlaylistScene("muted-video", wants_audio=True, audio=None)
        pl = _build_playlist([muted])
        pl.ensemble = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        self.assertEqual(pl.ensemble_coord.resolve_next_index(), 0)
        self.assertEqual(pl.ensemble.audio_holder, "other")
        self.assertNotIn("_audio_lock_held", muted.__dict__)

    def test_all_gated_waits_then_returns_when_freed(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")

        def free_after_delay():
            # Give the helper a chance to start its wait loop.
            threading.Event().wait(0.15)
            assert pl.ensemble is not None
            pl.ensemble.release_audio("other")

        threading.Thread(target=free_after_delay, daemon=True).start()

        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            idx = pl.ensemble_coord.resolve_next_index()
        self.assertEqual(idx, 0)
        assert pl.ensemble is not None
        self.assertEqual(pl.ensemble.audio_holder, "sys")

    def test_stop_event_exits_wait_loop(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        threading.Timer(0.05, pl.stop_event.set).start()
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            idx = pl.ensemble_coord.resolve_next_index()
        self.assertIsNone(idx)


class _WaitSignalEvent(threading.Event):
    """A stop event that says when somebody first sleeps on it, which is how a
    test knows a wait loop has taken its place without guessing at a delay."""

    def __init__(self) -> None:
        super().__init__()
        self.slept = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.slept.set()
        return super().wait(timeout)


class FairAudioHandoffTest(unittest.TestCase):
    """The ensemble audio slot goes to whoever has waited longest, so a holder
    that releases and claims again at once cannot starve a waiting system."""

    def _ensemble(self, names):
        return Ensemble(stacks=[fake_system_stack(n) for n in names], stop_event=threading.Event())

    def _waiting_playlist(self, scenes):
        pl = _build_playlist(scenes)
        pl.stop_event = _WaitSignalEvent()
        ens = self._ensemble(["sys", "other", "third"])
        pl.ensemble = ens
        ens.stop_event = pl.stop_event
        ens.try_claim_audio("other")
        return pl, ens

    def _in_thread(self, pl, fn):
        result: list = []
        t = threading.Thread(target=lambda: result.append(fn()))
        t.start()

        def finish() -> None:
            pl.stop_event.set()
            t.join(5)

        self.addCleanup(finish)
        self.assertTrue(pl.stop_event.slept.wait(5), "the waiter never went to sleep")
        return t, result

    def test_a_free_slot_goes_to_the_longest_waiter(self):
        ens = self._ensemble(["a", "b", "c"])
        ens.try_claim_audio("a")
        ens.join_audio_queue("b")
        ens.join_audio_queue("c")
        ens.release_audio("a")
        self.assertFalse(ens.try_claim_audio("a"))
        self.assertFalse(ens.try_claim_audio("c"))
        self.assertTrue(ens.try_claim_audio("b"))
        self.assertEqual(ens.audio_queue, ["c"])

    def test_joining_twice_keeps_the_first_place(self):
        ens = self._ensemble(["a", "b"])
        ens.join_audio_queue("a")
        ens.join_audio_queue("b")
        ens.join_audio_queue("a")
        self.assertEqual(ens.audio_queue, ["a", "b"])

    def test_the_holder_re_claiming_its_own_slot_ignores_the_line(self):
        ens = self._ensemble(["a", "b"])
        ens.try_claim_audio("a")
        ens.join_audio_queue("b")
        self.assertTrue(ens.try_claim_audio("a"))

    def test_a_holder_that_releases_and_claims_again_cannot_starve_wait_for_audio_claim(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl, ens = self._waiting_playlist([scene])
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            t, result = self._in_thread(pl, lambda: pl.ensemble_coord.wait_for_audio_claim(scene))
            ens.release_audio("other")
            self.assertFalse(
                ens.try_claim_audio("other"),
                "the holder took the slot back from the waiter",
            )
            t.join(5)
        self.assertEqual(result, [True])
        self.assertEqual(ens.audio_holder, "sys")
        self.assertEqual(ens.audio_queue, [])

    def test_a_holder_that_releases_and_claims_again_cannot_starve_resolve_next_index(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl, ens = self._waiting_playlist([scene])
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            t, result = self._in_thread(pl, pl.ensemble_coord.resolve_next_index)
            ens.release_audio("other")
            self.assertFalse(
                ens.try_claim_audio("other"),
                "the holder took the slot back from the waiter",
            )
            t.join(5)
        self.assertEqual(result, [0])
        self.assertEqual(ens.audio_holder, "sys")
        self.assertEqual(ens.audio_queue, [])

    def test_a_waiter_that_stops_leaves_the_line(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl, ens = self._waiting_playlist([scene])
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            t, result = self._in_thread(pl, lambda: pl.ensemble_coord.wait_for_audio_claim(scene))
            self.assertEqual(ens.audio_queue, ["sys"])
            pl.stop_event.set()
            t.join(5)
        self.assertEqual(result, [False])
        self.assertEqual(ens.audio_queue, [])
        ens.release_audio("other")
        self.assertTrue(ens.try_claim_audio("third"))

    def test_a_resolver_that_stops_leaves_the_line(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl, ens = self._waiting_playlist([scene])
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            t, result = self._in_thread(pl, pl.ensemble_coord.resolve_next_index)
            self.assertEqual(ens.audio_queue, ["sys"])
            pl.stop_event.set()
            t.join(5)
        self.assertEqual(result, [None])
        self.assertEqual(ens.audio_queue, [])
        ens.release_audio("other")
        self.assertTrue(ens.try_claim_audio("third"))

    def test_a_rotation_with_a_runnable_scene_skips_without_joining_the_line(self):
        gated = FakePlaylistScene("video", wants_audio=True)
        live = FakePlaylistScene("live", wants_audio=False)
        pl = _build_playlist([gated, live])
        ens = self._ensemble(["sys", "other"])
        pl.ensemble = ens
        ens.try_claim_audio("other")
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            self.assertEqual(pl.ensemble_coord.resolve_next_index(), 1)
        self.assertEqual(ens.audio_queue, [])


class SafeTeardownReleasesLockTest(unittest.TestCase):
    def test_teardown_releases_audio_slot_when_flag_set(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[fake_system_stack("sys")], stop_event=pl.stop_event)
        pl.ensemble.try_claim_audio("sys")
        scene.__dict__["_audio_lock_held"] = True

        pl.safe_teardown(scene)
        self.assertIsNone(pl.ensemble.audio_holder)
        self.assertFalse(scene.__dict__["_audio_lock_held"])

    def test_teardown_does_not_release_when_flag_unset(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        # scene didn't claim — _audio_lock_held is not set on it.
        pl.safe_teardown(scene)
        self.assertEqual(pl.ensemble.audio_holder, "other")

    def test_teardown_releases_even_when_scene_teardown_raises(self):
        class Boom(FakePlaylistScene):
            def teardown(self):
                raise RuntimeError("boom")

        scene = Boom("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[fake_system_stack("sys")], stop_event=pl.stop_event)
        pl.ensemble.try_claim_audio("sys")
        scene.__dict__["_audio_lock_held"] = True

        with self.assertLogs("c64cast.app.playlist", level="ERROR"):
            pl.safe_teardown(scene)
        self.assertIsNone(pl.ensemble.audio_holder)


class _ContendingScene(FakeScene):
    def competes_for_audio_lock(self) -> bool:
        return True


class _SilentScene(FakeScene):
    def competes_for_audio_lock(self) -> bool:
        return False


class DroppedCardReleasesTheSlotTest(unittest.TestCase):
    """An "UP NEXT" card holds the ensemble audio slot for the scene it
    announces. Anything that drops the card instead of playing that scene
    has to let the slot go, or a waiting system is held out for as long as
    the pause, the reloaded playlist or the launched clip lasts."""

    def _card_up(self) -> tuple[Playlist, Ensemble]:
        api = FakeApi()
        pl = Playlist(
            [_ContendingScene("tune", frames_until_done=10_000), _SilentScene("live")],
            api,
            name="sys",
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=threading.Event(),
            interstitial_factory=lambda name: _SilentScene(
                f"UP NEXT {name}", frames_until_done=10_000
            ),
        )
        ens = Ensemble(
            stacks=[fake_system_stack("sys"), fake_system_stack("other")],
            stop_event=threading.Event(),
        )
        pl.ensemble = ens
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl._advance()
        self.assertIs(pl.current, pl._card)
        self.assertEqual(ens.audio_holder, "sys")
        ens.join_audio_queue("other")
        return pl, ens

    def _assert_other_gets_it(self, ens: Ensemble) -> None:
        self.assertIsNone(ens.audio_holder, "the dropped card kept the slot")
        self.assertTrue(ens.try_claim_audio("other"))

    def test_a_pause_during_a_card(self):
        pl, ens = self._card_up()
        pl.stop_event.set()
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl._handle_pause()
        self._assert_other_gets_it(ens)

    def test_a_reload_during_a_card(self):
        pl, ens = self._card_up()
        pl.request_reload([_SilentScene("a"), _SilentScene("b")])
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl._apply_reload()
        self._assert_other_gets_it(ens)

    def test_a_silent_clip_launched_during_a_card(self):
        pl, ens = self._card_up()
        self.assertTrue(pl.perf_swap_scene(_SilentScene("clip", frames_until_done=10_000)))
        self._assert_other_gets_it(ens)

    def test_the_run_ending_during_a_card(self):
        pl, ens = self._card_up()
        pl.stop_event.set()
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl.run()
        self._assert_other_gets_it(ens)

    def test_a_broadcast_interrupt_during_a_card(self):
        pl, ens = self._card_up()

        class _Follower(_SilentScene):
            def bind_orchestrator(self, orch, *, conductor: bool, index: int) -> None:
                pass

        class _Orch:
            def follower_scene_cfg_for(self, name: str) -> object:
                return object()

        ens.active_orchestrator = _Orch()  # type: ignore[assignment]
        pl.broadcast_interrupt = threading.Event()
        pl.broadcast_resume = threading.Event()
        pl.broadcast_resume.set()
        pl.build_follower_scene = lambda cfg: _Follower("follower")  # type: ignore[assignment]
        with self.assertLogs("c64cast.app.playlist", level="INFO"):
            pl.ensemble_coord.handle_broadcast_interrupt()
        self._assert_other_gets_it(ens)

    def test_a_restart_under_the_card_keeps_the_slot_for_its_scene(self):
        pl, ens = self._card_up()
        with self.assertLogs("c64cast.app.playlist", level="WARNING"):
            pl._set_up_again_after_restart()
        self.assertIs(pl.current, pl._card)
        self.assertEqual(ens.audio_holder, "sys")


class AudioOnlyEnsembleWarningTest(unittest.TestCase):
    def test_warns_when_every_scene_in_a_system_is_audio_bearing(self):
        cfg_a = cfgmod.Config()
        cfg_a.scenes = [cfgmod.SceneCfg(type="webcam", display="petscii")]
        cfg_b = cfgmod.Config()
        cfg_b.scenes = [
            cfgmod.SceneCfg(type="video", file="x.mp4"),
            cfgmod.SceneCfg(type="video", file="y.mp4"),
        ]
        with self.assertLogs("c64cast.app.config", level="WARNING") as cap:
            cfgmod._warn_audio_only_ensemble([cfg_a, cfg_b], ["a", "b"])
        joined = "\n".join(cap.output)
        self.assertIn("[b]", joined)
        self.assertNotIn("[a]", joined)

    def test_no_warning_for_mixed_playlist(self):
        cfg = cfgmod.Config()
        cfg.scenes = [
            cfgmod.SceneCfg(type="webcam", display="petscii"),
            cfgmod.SceneCfg(type="video", file="x.mp4"),
        ]
        # `assertNoLogs` is 3.10+; fall back to capturing and asserting empty.
        with self.assertLogs("c64cast.app.config", level="WARNING") as cap:
            cfgmod._warn_audio_only_ensemble([cfg], ["mixed"])
            # Emit a sentinel so assertLogs doesn't itself raise on no-output.
            import logging

            logging.getLogger("c64cast.app.config").warning("sentinel")
        self.assertEqual([line for line in cap.output if "sentinel" not in line], [])


if __name__ == "__main__":
    unittest.main()
