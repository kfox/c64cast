"""Tests for Playlist._handle_broadcast_interrupt + _run_one_frame.

These exercise the broadcast-interrupt path with a fake orchestrator
and fake follower scene factory — no real ensemble, no real orchestrator
subclass needed. The big_text-driven end-to-end story lives in commits
13–15."""
# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false
from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest.mock import MagicMock

from c64cast.ensemble import Ensemble, SystemStack
from c64cast.playlist import Playlist


def _fake_ensemble_stack(name: str) -> SystemStack:
    """A SystemStack whose only meaningful field for these tests is
    `name` — _handle_broadcast_interrupt only looks at the system
    name to compute system_index."""
    return SystemStack(
        name=name,
        cfg=MagicMock(name=f"cfg-{name}"),
        api=MagicMock(name=f"api-{name}"),
        audio=None, source=None,
        playlist=MagicMock(name=f"playlist-{name}"),
        key_poller=MagicMock(name=f"keyboard-{name}"),
        framebuffer=None, preview_window=None, recorder=None,
    )


class _FakeScene:
    """Minimal scene that lives in a playlist. process_frame returns True
    forever unless `set_done()` is called; setup/teardown record calls."""

    def __init__(self, name: str):
        self.name = name
        self.is_done = False
        self.duration_s = 10.0
        self.target_fps: float | None = None
        self.overlays: list = []
        # MagicMock's auto-spec returns a Mock for any attribute, which
        # _frame_time_for then tries to compare to 0 and explodes. Pin
        # the attributes the playlist actually reads to real values.
        self.display_mode = MagicMock()
        self.display_mode.default_target_fps = None
        self.display_mode.cycle_style.return_value = None
        self.setup_calls = 0
        self.teardown_calls = 0
        self.process_calls = 0
        self._still_active = True

    def setup(self):
        self.setup_calls += 1

    def teardown(self):
        self.teardown_calls += 1

    def process_frame(self, t: float):
        self.process_calls += 1
        return self._still_active


class _FakeOrchestrator:
    """Minimal Orchestrator-shaped fake. We don't import the real ABC
    to keep these tests independent of subclass registration."""

    def __init__(self):
        self.is_active_return = True
        self.snapshot_data: dict[str, Any] = {}
        self.follower_cfgs: dict[str, Any] = {}

    def is_active(self) -> bool:
        return self.is_active_return

    def follower_scene_cfg_for(self, name: str):
        return self.follower_cfgs.get(name, MagicMock(name=f"cfg-{name}"))

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_data


def _build_playlist(name: str = "follower") -> Playlist:
    initial_scene = _FakeScene("initial")
    initial_scene._still_active = True
    api = MagicMock()
    api.stats = {"writes": 0, "bytes": 0}
    api.format_write_latency.return_value = None
    pl = Playlist(
        scenes=[initial_scene, _FakeScene("second")],
        api=api,
        target_fps=60.0,
        heartbeat_interval=999.0,
        stop_event=threading.Event(),
        interstitial_factory=lambda nm: _FakeScene(f"interstitial:{nm}"),
        key_poller=None,
        name=name,
    )
    return pl


class RunOneFrameTest(unittest.TestCase):
    """_run_one_frame is the per-frame body extracted from run() —
    drives scene.process_frame + overlay process_frame + heartbeat +
    frame-drop logic. The broadcast loop reuses it, so the basics
    matter independently of the broadcast path."""

    def test_calls_scene_process_frame(self):
        pl = _build_playlist()
        scene = _FakeScene("s")
        pl._run_one_frame(scene, 0.0)
        self.assertEqual(scene.process_calls, 1)

    def test_sets_is_done_when_scene_returns_false(self):
        pl = _build_playlist()
        scene = _FakeScene("s")
        scene._still_active = False
        pl._run_one_frame(scene, 0.0)
        self.assertTrue(scene.is_done)

    def test_skip_event_forces_is_done_even_when_active(self):
        pl = _build_playlist()
        scene = _FakeScene("s")
        scene._still_active = True
        pl.skip_event.set()
        with self.assertLogs("c64cast.playlist", level="INFO"):
            pl._run_one_frame(scene, 0.0)
        self.assertTrue(scene.is_done)
        # skip_event is cleared after handling.
        self.assertFalse(pl.skip_event.is_set())


class BroadcastInterruptTest(unittest.TestCase):

    def _wire_broadcast(self, pl: Playlist, orch: _FakeOrchestrator,
                        follower_scene: _FakeScene) -> tuple[
                            threading.Event, threading.Event]:
        """Plumb the broadcast events + follower factory onto a playlist
        + a one-stack Ensemble holding `orch` as the active orchestrator.

        The ensemble has to include the playlist's name in its stack
        list because _handle_broadcast_interrupt looks up the playlist's
        index in `ensemble.system_names()` to stamp _system_index on
        the follower scene."""
        interrupt = threading.Event()
        resume = threading.Event()
        ens = Ensemble(stacks=[_fake_ensemble_stack(pl.name)],
                       stop_event=pl.stop_event)
        ens.active_orchestrator = orch  # type: ignore[assignment]
        pl.ensemble = ens
        pl._broadcast_interrupt = interrupt
        pl._broadcast_resume = resume
        pl.build_follower_scene = lambda cfg: follower_scene
        return interrupt, resume

    def test_interrupt_swaps_in_follower_and_resumes(self):
        pl = _build_playlist()
        # Pretend a scene was already running so teardown is exercised.
        initial = _FakeScene("initial")
        pl.current = initial
        pl.index = 1

        follower = _FakeScene("follower-scene")
        orch = _FakeOrchestrator()
        interrupt, resume = self._wire_broadcast(pl, orch, follower)

        # Schedule a resume after a few process_frame calls so the
        # broadcast loop exits naturally.
        def stop_after_three():
            while follower.process_calls < 3:
                pass
            resume.set()
        t = threading.Thread(target=stop_after_three, daemon=True)
        t.start()

        interrupt.set()
        with self.assertLogs("c64cast.playlist", level="INFO"):
            pl._handle_broadcast_interrupt()
        t.join(timeout=2.0)

        self.assertEqual(initial.teardown_calls, 1)
        self.assertEqual(follower.setup_calls, 1)
        self.assertGreaterEqual(follower.process_calls, 3)
        self.assertEqual(follower.teardown_calls, 1)
        self.assertEqual(pl.index, 1)
        self.assertIsNone(pl.current)
        # Events cleared so the next interrupt cycle starts clean.
        self.assertFalse(interrupt.is_set())
        self.assertFalse(resume.is_set())

    def test_interrupt_stamps_orchestrator_role_on_follower(self):
        pl = _build_playlist()
        follower = _FakeScene("follower")
        orch = _FakeOrchestrator()
        _, resume = self._wire_broadcast(pl, orch, follower)
        resume.set()   # exit broadcast loop immediately

        with self.assertLogs("c64cast.playlist", level="INFO"):
            pl._handle_broadcast_interrupt()

        self.assertIs(follower._orchestrator, orch)
        self.assertFalse(follower._is_conductor)

    def test_paused_playlist_is_force_resumed(self):
        # The follower being paused when the broadcast hits is an
        # acknowledged edge case: force-resume + run the broadcast +
        # leave un-paused after.
        pl = _build_playlist()
        pl.pause_event.set()
        follower = _FakeScene("follower")
        orch = _FakeOrchestrator()
        _, resume = self._wire_broadcast(pl, orch, follower)
        resume.set()

        with self.assertLogs("c64cast.playlist", level="INFO") as cap:
            pl._handle_broadcast_interrupt()

        self.assertFalse(pl.pause_event.is_set())
        # resume_event was set so a concurrent _handle_pause loop would
        # exit cleanly (the test doesn't run _handle_pause itself).
        self.assertTrue(pl.resume_event.is_set())
        self.assertTrue(any("force-resuming" in line for line in cap.output))

    def test_no_orchestrator_active_drops_interrupt(self):
        pl = _build_playlist()
        # Ensemble exists but active_orchestrator is None (stale event).
        pl.ensemble = Ensemble(stacks=[], stop_event=pl.stop_event)
        pl._broadcast_interrupt = threading.Event()
        pl._broadcast_resume = threading.Event()
        pl._broadcast_interrupt.set()
        # Should silently drop, not crash.
        pl._handle_broadcast_interrupt()
        self.assertFalse(pl._broadcast_interrupt.is_set())

    def test_no_factory_logs_error_and_returns(self):
        pl = _build_playlist()
        ens = Ensemble(stacks=[], stop_event=pl.stop_event)
        ens.active_orchestrator = _FakeOrchestrator()  # type: ignore[assignment]
        pl.ensemble = ens
        pl._broadcast_interrupt = threading.Event()
        pl._broadcast_resume = threading.Event()
        pl._broadcast_interrupt.set()
        # build_follower_scene is None — should log + bail without crash.
        with self.assertLogs("c64cast.playlist", level="ERROR") as cap:
            pl._handle_broadcast_interrupt()
        self.assertTrue(any("no follower scene factory" in line
                            for line in cap.output))

    def test_follower_with_orchestrate_cfg_does_not_re_install_conductor(self):
        # Regression for the phase-2 verification bug: when a follower's
        # fallback SceneCfg is the conductor's cfg (no local override),
        # cfg.orchestrate=true on the follower side would clobber the
        # follower stamps with a fresh conductor install. _safe_setup's
        # _maybe_install_conductor must skip when scene._orchestrator
        # is already set.
        from c64cast.config import SceneCfg
        pl = _build_playlist(name="follower")
        # Build the ensemble with our follower's name in it so the
        # broadcast machinery resolves indices cleanly.
        ens = Ensemble(stacks=[_fake_ensemble_stack("follower")],
                       stop_event=pl.stop_event)
        pl.ensemble = ens
        # Stamp a fake scene as if _handle_broadcast_interrupt already ran.
        scene = _FakeScene("conductor-cfg")
        scene._cfg = SceneCfg(type="blank", name="x", orchestrate=True)
        # type: ignore[attr-defined]
        scene._orchestrator = _FakeOrchestrator()  # type: ignore[attr-defined]
        scene._is_conductor = False  # type: ignore[attr-defined]
        scene._system_index = 0  # type: ignore[attr-defined]
        # Calling _maybe_install_conductor must be a no-op — the scene
        # already has an orchestrator and is in follower role.
        pl._maybe_install_conductor(scene)
        # _is_conductor must NOT have been flipped back to True.
        self.assertFalse(scene._is_conductor)
        # active_orchestrator must NOT have been touched.
        self.assertIsNone(ens.active_orchestrator)

    def test_conductor_teardown_clears_per_scene_stamps(self):
        # The conductor's Scene instance is reused across playlist loops
        # (and across single-scene mode iterations). _safe_teardown must
        # therefore clear scene._orchestrator + scene._is_conductor so
        # the next _safe_setup → _maybe_install_conductor re-installs a
        # fresh orchestrator. Without this, ensemble.active_orchestrator
        # stays None on the 2nd+ broadcast and every follower drops the
        # interrupt as "no active orch".
        from c64cast.config import SceneCfg
        pl = _build_playlist(name="conductor")
        ens = Ensemble(stacks=[_fake_ensemble_stack("conductor")],
                       stop_event=pl.stop_event)
        pl.ensemble = ens
        scene = _FakeScene("morning-hello")
        scene._cfg = SceneCfg(type="blank", name="morning-hello",
                              orchestrate=True)
        # Simulate a prior broadcast: orchestrator wired up, marked active.
        orch = _FakeOrchestrator()
        scene._orchestrator = orch  # type: ignore[attr-defined]
        scene._is_conductor = True  # type: ignore[attr-defined]
        ens.active_orchestrator = orch  # type: ignore[assignment]

        pl._safe_teardown(scene)

        self.assertIsNone(ens.active_orchestrator)
        self.assertIsNone(scene.__dict__.get("_orchestrator"))
        self.assertFalse(scene.__dict__.get("_is_conductor"))

    def test_conductor_re_setup_reinstalls_orchestrator(self):
        # End-to-end: after teardown of a conductor scene, calling
        # _maybe_install_conductor again on the SAME scene instance must
        # produce a fresh orchestrator wired into ensemble.active_orchestrator
        # (regression for the "subsequent broadcasts only paint the rightmost
        # screen" bug — followers couldn't see the broadcast because the
        # ensemble slot was empty after the first run).
        from c64cast.config import SceneCfg
        pl = _build_playlist(name="conductor")
        ens = Ensemble(stacks=[_fake_ensemble_stack("conductor")],
                       stop_event=pl.stop_event)
        pl.ensemble = ens
        scene = _FakeScene("morning-hello")
        scene._cfg = SceneCfg(type="blank", name="morning-hello",
                              orchestrate=True)

        # The Playlist's _maybe_install_conductor needs an Orchestrator
        # subclass that claims this cfg. Register a minimal one for the
        # test (the registry is global; we clean up after).
        from c64cast import orchestrator as orch_mod

        class _TestOrch(orch_mod.Orchestrator):
            @classmethod
            def claims(cls, scene_cfg):
                return scene_cfg.name == "morning-hello"

            def snapshot(self):
                return {}

        orch_mod._REGISTRY.append(_TestOrch)
        try:
            # First setup: fresh install.
            pl._maybe_install_conductor(scene)
            first_orch = ens.active_orchestrator
            self.assertIsInstance(first_orch, _TestOrch)
            self.assertIs(scene.__dict__["_orchestrator"], first_orch)
            self.assertTrue(scene.__dict__["_is_conductor"])

            # Simulate end-of-broadcast teardown.
            pl._safe_teardown(scene)
            self.assertIsNone(ens.active_orchestrator)

            # Second setup on the same Scene instance: must install a
            # FRESH orchestrator into the ensemble slot, not silently
            # leave it None.
            pl._maybe_install_conductor(scene)
            second_orch = ens.active_orchestrator
            self.assertIsInstance(second_orch, _TestOrch)
            self.assertIsNot(second_orch, first_orch)
            self.assertIs(scene.__dict__["_orchestrator"], second_orch)
            self.assertTrue(scene.__dict__["_is_conductor"])
        finally:
            orch_mod._REGISTRY.remove(_TestOrch)

    def test_stop_event_exits_broadcast_loop(self):
        pl = _build_playlist()
        follower = _FakeScene("follower")
        orch = _FakeOrchestrator()
        _, _ = self._wire_broadcast(pl, orch, follower)
        # Don't set resume; set stop_event instead.
        pl.stop_event.set()
        with self.assertLogs("c64cast.playlist", level="INFO"):
            pl._handle_broadcast_interrupt()
        # Follower was set up, ran zero or more frames, and was torn down.
        self.assertEqual(follower.setup_calls, 1)
        self.assertEqual(follower.teardown_calls, 1)


if __name__ == "__main__":
    unittest.main()
