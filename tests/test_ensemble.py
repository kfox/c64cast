"""Tests for the Ensemble registry.

The orchestrator-related Ensemble methods land in phase 2; this file
covers the bare registry shape (stacks list + stop_event + name lookup)."""

from __future__ import annotations

import threading
import unittest

from _fakes import fake_system_stack

from c64cast.app.ensemble import Ensemble


class EnsembleRegistryTest(unittest.TestCase):
    def test_system_names_preserves_order(self):
        stop = threading.Event()
        ens = Ensemble(
            stacks=[
                fake_system_stack("left"),
                fake_system_stack("middle"),
                fake_system_stack("right"),
            ],
            stop_event=stop,
        )
        self.assertEqual(ens.system_names(), ["left", "middle", "right"])

    def test_stack_lookup_returns_named_stack(self):
        stop = threading.Event()
        left = fake_system_stack("left")
        right = fake_system_stack("right")
        ens = Ensemble(stacks=[left, right], stop_event=stop)
        self.assertIs(ens.stack("left"), left)
        self.assertIs(ens.stack("right"), right)

    def test_stack_lookup_raises_key_error_on_unknown(self):
        ens = Ensemble(stacks=[fake_system_stack("left")], stop_event=threading.Event())
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
        ens.stacks = [fake_system_stack("solo")]
        self.assertEqual(ens.system_names(), ["solo"])
        self.assertIs(ens.stop_event, stop)


if __name__ == "__main__":
    unittest.main()
