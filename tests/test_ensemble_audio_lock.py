"""Tests for ensemble audio coordination.

Three layers:
  1. `Ensemble.try_claim_audio` / `release_audio` — the atomic primitive.
  2. `config.build_scene(..., is_ensemble=True)` — live scenes (webcam,
     blank) build with audio=None so they can't compete for the SID.
  3. `Playlist._resolve_next_index` + `_safe_teardown` — gating
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

from c64cast import config as cfgmod
from c64cast.ensemble import Ensemble, SystemStack
from c64cast.playlist import Playlist
from c64cast.scenes import BlankScene, Scene, VideoScene, WebcamScene

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI  # noqa: E402

# ---------------------------------------------------------------------------
# Layer 1: Ensemble.try_claim_audio / release_audio
# ---------------------------------------------------------------------------


def _fake_stack(name: str) -> SystemStack:
    return SystemStack(
        name=name,
        cfg=MagicMock(name=f"cfg-{name}"),
        api=MagicMock(name=f"api-{name}"),
        audio=None,
        source=None,
        playlist=MagicMock(name=f"playlist-{name}"),
        key_poller=MagicMock(name=f"keyboard-{name}"),
        framebuffer=None,
        preview_window=None,
        recorder=None,
    )


class EnsembleAudioLockTest(unittest.TestCase):
    def _ensemble(self, names):
        return Ensemble(stacks=[_fake_stack(n) for n in names], stop_event=threading.Event())

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
        # Repeat setup() calls (single-scene loop, follower restore) must
        # not deadlock on a slot we already own.
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
        # Teardown paths must tolerate a stale release — never raise,
        # never clobber the real holder.
        ens = self._ensemble(["a", "b"])
        ens.try_claim_audio("a")
        ens.release_audio("b")  # doesn't hold the slot
        self.assertEqual(ens.audio_holder, "a")

    def test_release_when_unheld_is_noop(self):
        ens = self._ensemble(["a"])
        ens.release_audio("a")  # no-op, no exception
        self.assertIsNone(ens.audio_holder)

    def test_concurrent_claims_only_one_wins(self):
        # 32 threads race to claim; exactly one should see True.
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


# ---------------------------------------------------------------------------
# Layer 2: build_scene(..., is_ensemble=True) live-scene suppression
# ---------------------------------------------------------------------------


class EnsembleLiveSceneSuppressionTest(unittest.TestCase):
    def setUp(self):
        from c64cast.api import Ultimate64API
        from c64cast.audio import AudioStreamer
        from c64cast.video import WebcamSource

        self.api = cast(Ultimate64API, FakeAPI())
        self.audio_sentinel = cast(AudioStreamer, object())
        self.source = cast(WebcamSource, object())
        self.cfg = cfgmod.Config()

    def test_webcam_audio_suppressed_in_ensemble_mode(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(
            s, self.cfg, self.api, self.audio_sentinel, self.source, is_ensemble=True
        )
        self.assertIsNone(scene.audio, "live webcam scene must not hold audio in ensemble")

    def test_blank_audio_suppressed_in_ensemble_mode(self):
        s = cfgmod.SceneCfg(type="blank")
        scene = cfgmod.build_scene(
            s, self.cfg, self.api, self.audio_sentinel, None, is_ensemble=True
        )
        self.assertIsNone(scene.audio)

    def test_webcam_explicit_audio_true_is_logged_when_suppressed(self):
        # If the user typed `audio = true` on a live scene, surface that
        # we silently overrode it — debug-find-later silence would be
        # confusing.
        s = cfgmod.SceneCfg(type="webcam", display="petscii", audio=True)
        with self.assertLogs("c64cast.config", level="INFO") as cap:
            scene = cfgmod.build_scene(
                s, self.cfg, self.api, self.audio_sentinel, self.source, is_ensemble=True
            )
        self.assertIsNone(scene.audio)
        self.assertTrue(any("audio suppressed in ensemble" in line for line in cap.output))

    def test_single_system_mode_unaffected(self):
        # is_ensemble defaults False; behavior matches the existing tests.
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIs(scene.audio, self.audio_sentinel)


# ---------------------------------------------------------------------------
# Layer 2b: WANTS_AUDIO_LOCK flags on the right scene classes
# ---------------------------------------------------------------------------


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
        # Local import — waveform pulls in songlengths which is heavier
        # than the live scenes.
        from c64cast.waveform import WaveformScene

        self.assertTrue(WaveformScene.WANTS_AUDIO_LOCK)

    def test_midi_scene_claims(self):
        from c64cast.midi_scene import MidiScene

        self.assertTrue(MidiScene.WANTS_AUDIO_LOCK)


# ---------------------------------------------------------------------------
# Layer 2c: competes_for_audio_lock() — instance-level contention
# ---------------------------------------------------------------------------


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
        # WaveformScene drives the SID directly, so it contends whether
        # or not an AudioStreamer was wired in (global [audio] off).
        from c64cast.waveform import WaveformScene

        wf = WaveformScene.__new__(WaveformScene)
        wf.audio = None
        self.assertTrue(wf.competes_for_audio_lock())

    def test_midi_competes_even_without_streamer(self):
        from c64cast.midi_scene import MidiScene

        midi = MidiScene.__new__(MidiScene)
        midi.audio = None
        self.assertTrue(midi.competes_for_audio_lock())


# ---------------------------------------------------------------------------
# Layer 3: Playlist gating + lock release
# ---------------------------------------------------------------------------


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
        # The gate is a no-op outside ensemble mode — single-system runs
        # never instantiate an Ensemble, so the helper must not try to
        # touch one.
        pl = _build_playlist([FakePlaylistScene("a"), FakePlaylistScene("b")])
        pl.index = 1
        self.assertEqual(pl._resolve_next_index(), 1)

    def test_non_audio_scene_passes_through(self):
        pl = _build_playlist(
            [FakePlaylistScene("a", wants_audio=False), FakePlaylistScene("b", wants_audio=True)]
        )
        pl.ensemble = Ensemble(stacks=[_fake_stack("sys")], stop_event=pl.stop_event)
        self.assertEqual(pl._resolve_next_index(), 0)

    def test_audio_scene_claims_lock_when_free(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[_fake_stack("sys")], stop_event=pl.stop_event)
        self.assertEqual(pl._resolve_next_index(), 0)
        self.assertEqual(pl.ensemble.audio_holder, "sys")
        self.assertTrue(scene.__dict__["_audio_lock_held"])

    def test_audio_scene_skipped_when_lock_held_elsewhere(self):
        # Two scenes: a held-elsewhere video then a live scene.
        # Helper must skip past slot 0 and land on slot 1.
        comm = FakePlaylistScene("video", wants_audio=True)
        live = FakePlaylistScene("live", wants_audio=False)
        pl = _build_playlist([comm, live])
        pl.ensemble = Ensemble(
            stacks=[_fake_stack("sys"), _fake_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            idx = pl._resolve_next_index()
        self.assertEqual(idx, 1)
        self.assertTrue(any("skipping audio-bearing" in line for line in cap.output))

    def test_muted_audio_scene_passes_through_when_lock_held(self):
        # An audio-capable scene with audio disabled (audio=None) does
        # not contend — even with the slot held elsewhere it's returned
        # directly and never claims the lock.
        muted = FakePlaylistScene("muted-video", wants_audio=True, audio=None)
        pl = _build_playlist([muted])
        pl.ensemble = Ensemble(
            stacks=[_fake_stack("sys"), _fake_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        self.assertEqual(pl._resolve_next_index(), 0)
        self.assertEqual(pl.ensemble.audio_holder, "other")
        self.assertNotIn("_audio_lock_held", muted.__dict__)

    def test_all_gated_waits_then_returns_when_freed(self):
        # Single audio-bearing scene, lock held elsewhere. Free it from
        # another thread after a short delay; helper should pick it up.
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[_fake_stack("sys"), _fake_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")

        def free_after_delay():
            # Give the helper a chance to start its wait loop.
            threading.Event().wait(0.15)
            assert pl.ensemble is not None
            pl.ensemble.release_audio("other")

        threading.Thread(target=free_after_delay, daemon=True).start()

        with self.assertLogs("c64cast.playlist", level="INFO"):
            idx = pl._resolve_next_index()
        self.assertEqual(idx, 0)
        assert pl.ensemble is not None
        self.assertEqual(pl.ensemble.audio_holder, "sys")

    def test_stop_event_exits_wait_loop(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[_fake_stack("sys"), _fake_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        # Fire stop_event almost immediately.
        threading.Timer(0.05, pl.stop_event.set).start()
        with self.assertLogs("c64cast.playlist", level="INFO"):
            idx = pl._resolve_next_index()
        self.assertIsNone(idx)


class SafeTeardownReleasesLockTest(unittest.TestCase):
    def test_teardown_releases_audio_slot_when_flag_set(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[_fake_stack("sys")], stop_event=pl.stop_event)
        pl.ensemble.try_claim_audio("sys")
        scene.__dict__["_audio_lock_held"] = True

        pl._safe_teardown(scene)
        self.assertIsNone(pl.ensemble.audio_holder)
        self.assertFalse(scene.__dict__["_audio_lock_held"])

    def test_teardown_does_not_release_when_flag_unset(self):
        scene = FakePlaylistScene("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(
            stacks=[_fake_stack("sys"), _fake_stack("other")], stop_event=pl.stop_event
        )
        pl.ensemble.try_claim_audio("other")
        # scene didn't claim — _audio_lock_held is not set on it.
        pl._safe_teardown(scene)
        # Other system's claim is untouched.
        self.assertEqual(pl.ensemble.audio_holder, "other")

    def test_teardown_releases_even_when_scene_teardown_raises(self):
        class Boom(FakePlaylistScene):
            def teardown(self):
                raise RuntimeError("boom")

        scene = Boom("video", wants_audio=True)
        pl = _build_playlist([scene])
        pl.ensemble = Ensemble(stacks=[_fake_stack("sys")], stop_event=pl.stop_event)
        pl.ensemble.try_claim_audio("sys")
        scene.__dict__["_audio_lock_held"] = True

        # Should swallow the teardown exception AND still release.
        with self.assertLogs("c64cast.playlist", level="ERROR"):
            pl._safe_teardown(scene)
        self.assertIsNone(pl.ensemble.audio_holder)


# ---------------------------------------------------------------------------
# Layer 4: load-time warning for audio-only ensemble playlists
# ---------------------------------------------------------------------------


class AudioOnlyEnsembleWarningTest(unittest.TestCase):
    def test_warns_when_every_scene_in_a_system_is_audio_bearing(self):
        cfg_a = cfgmod.Config()
        cfg_a.scenes = [cfgmod.SceneCfg(type="webcam", display="petscii")]
        cfg_b = cfgmod.Config()
        cfg_b.scenes = [
            cfgmod.SceneCfg(type="video", file="x.mp4"),
            cfgmod.SceneCfg(type="video", file="y.mp4"),
        ]
        with self.assertLogs("c64cast.config", level="WARNING") as cap:
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
        with self.assertLogs("c64cast.config", level="WARNING") as cap:
            cfgmod._warn_audio_only_ensemble([cfg], ["mixed"])
            # Emit a sentinel so assertLogs doesn't itself raise on no-output.
            import logging

            logging.getLogger("c64cast.config").warning("sentinel")
        self.assertEqual([line for line in cap.output if "sentinel" not in line], [])


if __name__ == "__main__":
    unittest.main()
