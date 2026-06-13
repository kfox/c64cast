"""Tests for the Orchestrator ABC + registry.

Subclasses (BigTextSpanOrchestrator etc.) get their own files; this
file covers the framework: registration, claims-based dispatch, begin/
end event flow, follower scene cfg resolution. We define throwaway
test-only subclasses inline since no production subclasses exist yet."""
# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest.mock import MagicMock

from c64cast import orchestrator as orch_mod
from c64cast.config import SceneCfg
from c64cast.ensemble import Ensemble, SystemStack
from c64cast.orchestrator import (
    Orchestrator,
    OrchestratorError,
    register_orchestrator,
    resolve_orchestrator,
)


def _fake_stack(name: str, scenes: list[SceneCfg] | None = None) -> SystemStack:
    cfg = MagicMock(name=f"cfg-{name}")
    cfg.scenes = scenes or []
    return SystemStack(
        name=name, cfg=cfg,
        api=MagicMock(name=f"api-{name}"),
        audio=None, source=None,
        playlist=MagicMock(name=f"playlist-{name}"),
        key_poller=MagicMock(name=f"keyboard-{name}"),
        framebuffer=None, preview_window=None, recorder=None,
    )


def _ensemble(*names: str,
              stacks_overrides: dict[str, list[SceneCfg]] | None = None
              ) -> Ensemble:
    overrides = stacks_overrides or {}
    stacks = [_fake_stack(n, overrides.get(n, [])) for n in names]
    return Ensemble(stacks=stacks, stop_event=threading.Event())


class _StubSpan(Orchestrator):
    """Test-only span-style orchestrator that only claims blank scenes
    named 'broadcast'."""

    @classmethod
    def claims(cls, scene_cfg: SceneCfg) -> bool:
        return scene_cfg.type == "blank" and scene_cfg.name == "broadcast"

    def snapshot(self) -> dict[str, Any]:
        return {"active": self.is_active()}


class RegistryTest(unittest.TestCase):

    def setUp(self):
        # Snapshot + clear the registry so subclasses defined elsewhere
        # don't leak into these tests, and our test-only registrations
        # don't leak out.
        self._saved = orch_mod._REGISTRY[:]
        orch_mod._REGISTRY.clear()

    def tearDown(self):
        orch_mod._REGISTRY.clear()
        orch_mod._REGISTRY.extend(self._saved)

    def test_no_claim_raises(self):
        cfg = SceneCfg(type="blank", name="unknown")
        with self.assertRaises(OrchestratorError) as cm:
            resolve_orchestrator(cfg)
        self.assertIn("no orchestrator subclass claims", str(cm.exception))

    def test_single_claim_returns_subclass(self):
        register_orchestrator(_StubSpan)
        cfg = SceneCfg(type="blank", name="broadcast")
        self.assertIs(resolve_orchestrator(cfg), _StubSpan)

    def test_ambiguous_claim_raises(self):
        register_orchestrator(_StubSpan)

        class _OtherSpan(Orchestrator):
            @classmethod
            def claims(cls, scene_cfg: SceneCfg) -> bool:
                return True   # claims everything → conflicts with _StubSpan

            def snapshot(self) -> dict[str, Any]:
                return {}

        register_orchestrator(_OtherSpan)
        cfg = SceneCfg(type="blank", name="broadcast")
        with self.assertRaises(OrchestratorError) as cm:
            resolve_orchestrator(cfg)
        self.assertIn("ambiguous", str(cm.exception))

    def test_register_decorator_returns_class(self):
        # @register_orchestrator must be transparent — class identity
        # preserved so callers can still reference it normally.
        @register_orchestrator
        class _T(Orchestrator):
            @classmethod
            def claims(cls, scene_cfg: SceneCfg) -> bool:
                return False

            def snapshot(self) -> dict[str, Any]:
                return {}

        self.assertIn(_T, orch_mod.registered_orchestrators())


class BeginEndProtocolTest(unittest.TestCase):

    def _orch(self, conductor: str = "right") -> _StubSpan:
        ens = _ensemble("left", "middle", "right")
        return _StubSpan(ens, conductor)

    def test_begin_sets_active_and_fires_follower_events(self):
        orch = self._orch()
        cfg = SceneCfg(type="blank", name="broadcast")
        # Followers (left, middle) start with un-set interrupt events.
        self.assertFalse(orch.interrupt_event("left").is_set())
        self.assertFalse(orch.interrupt_event("middle").is_set())
        self.assertTrue(orch.begin(cfg))
        self.assertTrue(orch.is_active())
        self.assertTrue(orch.interrupt_event("left").is_set())
        self.assertTrue(orch.interrupt_event("middle").is_set())

    def test_begin_while_active_returns_false(self):
        orch = self._orch()
        cfg = SceneCfg(type="blank", name="broadcast")
        self.assertTrue(orch.begin(cfg))
        # Second begin without an intervening end is refused.
        self.assertFalse(orch.begin(cfg))
        # State unchanged.
        self.assertTrue(orch.is_active())

    def test_end_clears_active_and_fires_resume(self):
        orch = self._orch()
        cfg = SceneCfg(type="blank", name="broadcast")
        orch.begin(cfg)
        self.assertFalse(orch.resume_event("left").is_set())
        orch.end()
        self.assertFalse(orch.is_active())
        self.assertTrue(orch.resume_event("left").is_set())
        self.assertTrue(orch.resume_event("middle").is_set())

    def test_end_when_inactive_is_noop(self):
        orch = self._orch()
        # Pre-begin: resume events are un-set. end() should leave them.
        orch.end()
        self.assertFalse(orch.is_active())
        self.assertFalse(orch.resume_event("left").is_set())

    def test_conductor_has_no_self_events(self):
        # The conductor itself isn't a follower; no interrupt/resume
        # event is allocated for it. KeyError if someone tries.
        orch = self._orch(conductor="right")
        with self.assertRaises(KeyError):
            orch.interrupt_event("right")
        with self.assertRaises(KeyError):
            orch.resume_event("right")


class FollowerSceneResolutionTest(unittest.TestCase):

    def test_follower_with_matching_name_uses_local_cfg(self):
        # The follower has its own scene named "broadcast" — orchestrator
        # should return that one so per-system visual overrides apply.
        local = SceneCfg(type="blank", name="broadcast",
                         border=5, background=7)
        ens = _ensemble("left", "right",
                        stacks_overrides={"left": [local]})
        orch = _StubSpan(ens, "right")
        conductor_cfg = SceneCfg(type="blank", name="broadcast",
                                 border=0, background=0)
        orch.begin(conductor_cfg)
        self.assertIs(orch.follower_scene_cfg_for("left"), local)

    def test_follower_without_matching_name_falls_back_to_conductor_cfg(self):
        # left has no scene named "broadcast" — fall back to conductor's.
        ens = _ensemble("left", "right")
        orch = _StubSpan(ens, "right")
        conductor_cfg = SceneCfg(type="blank", name="broadcast")
        orch.begin(conductor_cfg)
        self.assertIs(orch.follower_scene_cfg_for("left"), conductor_cfg)

    def test_follower_with_own_orchestrate_true_is_skipped(self):
        # If a follower happens to also have orchestrate=true on a scene
        # with the same name, that scene is NOT picked as the override —
        # we don't want two conductors. Fall back to the broadcast cfg.
        local = SceneCfg(type="blank", name="broadcast", orchestrate=True)
        ens = _ensemble("left", "right",
                        stacks_overrides={"left": [local]})
        orch = _StubSpan(ens, "right")
        conductor_cfg = SceneCfg(type="blank", name="broadcast")
        orch.begin(conductor_cfg)
        self.assertIs(orch.follower_scene_cfg_for("left"), conductor_cfg)

    def test_follower_only_marked_scene_is_picked_as_override(self):
        # The recommended pattern: the follower's local "broadcast" cfg
        # is marked follower_only=true so it stays out of the regular
        # rotation, but follower_scene_cfg_for still finds it by name.
        local = SceneCfg(type="blank", name="broadcast",
                         border=5, follower_only=True)
        ens = _ensemble("left", "right",
                        stacks_overrides={"left": [local]})
        orch = _StubSpan(ens, "right")
        conductor_cfg = SceneCfg(type="blank", name="broadcast")
        orch.begin(conductor_cfg)
        self.assertIs(orch.follower_scene_cfg_for("left"), local)

    def test_follower_resolution_when_inactive_raises(self):
        ens = _ensemble("left", "right")
        orch = _StubSpan(ens, "right")
        with self.assertRaises(OrchestratorError):
            orch.follower_scene_cfg_for("left")


if __name__ == "__main__":
    unittest.main()
