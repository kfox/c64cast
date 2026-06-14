"""Tests for the Ensemble registry.

The orchestrator-related Ensemble methods land in phase 2; this file
covers the bare registry shape (stacks list + stop_event + name lookup)."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from c64cast.ensemble import Ensemble, SystemStack


def _fake_stack(name: str) -> SystemStack:
    """A SystemStack with every non-trivial field mocked — Ensemble only
    needs the `name` field; other fields aren't exercised here."""
    return SystemStack(
        name=name,
        cfg=MagicMock(name="cfg"),
        api=MagicMock(name="api"),
        audio=None,
        source=None,
        playlist=MagicMock(name="playlist"),
        key_poller=MagicMock(name="key_poller"),
        framebuffer=None,
        preview_window=None,
        recorder=None,
    )


class EnsembleRegistryTest(unittest.TestCase):
    def test_system_names_preserves_order(self):
        stop = threading.Event()
        ens = Ensemble(
            stacks=[
                _fake_stack("left"),
                _fake_stack("middle"),
                _fake_stack("right"),
            ],
            stop_event=stop,
        )
        self.assertEqual(ens.system_names(), ["left", "middle", "right"])

    def test_stack_lookup_returns_named_stack(self):
        stop = threading.Event()
        left = _fake_stack("left")
        right = _fake_stack("right")
        ens = Ensemble(stacks=[left, right], stop_event=stop)
        self.assertIs(ens.stack("left"), left)
        self.assertIs(ens.stack("right"), right)

    def test_stack_lookup_raises_key_error_on_unknown(self):
        ens = Ensemble(stacks=[_fake_stack("left")], stop_event=threading.Event())
        with self.assertRaises(KeyError) as cm:
            ens.stack("nope")
        self.assertIn("nope", str(cm.exception))
        self.assertIn("left", str(cm.exception))  # known list surfaced

    def test_two_phase_construction(self):
        # Ensemble can be allocated empty and have stacks assigned after
        # build_stack returns — Playlists need the stop_event at build time.
        stop = threading.Event()
        ens = Ensemble(stacks=[], stop_event=stop)
        self.assertEqual(ens.system_names(), [])
        ens.stacks = [_fake_stack("solo")]
        self.assertEqual(ens.system_names(), ["solo"])
        self.assertIs(ens.stop_event, stop)


if __name__ == "__main__":
    unittest.main()
