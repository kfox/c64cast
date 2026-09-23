"""Tests for the RNG sandbox — the guard that reseeds both process-wide
generators before every test, so a seed one test left behind cannot decide
what the next one draws (see tests/_rng_sandbox.py)."""

from __future__ import annotations

import random
import sys
import unittest
from unittest.mock import patch

import _rng_sandbox
import numpy as np
from _rng_sandbox import reseed, seed_for


class SeedTest(unittest.TestCase):
    def test_a_test_id_maps_to_one_seed_whatever_process_asks(self):
        """Pinned as a literal because that is the property: a seed drawn from
        `hash()` would be salted per process by PYTHONHASHSEED and this
        assertion would pass in some runs and fail in others, which is the
        order dependence the guard exists to remove, moved."""
        self.assertEqual(seed_for("tests.test_thing.Case.test_one"), 3345823717130224567)

    def test_different_test_ids_get_different_seeds(self):
        ids = [f"tests.test_thing.Case.test_{n}" for n in range(200)]
        self.assertEqual(len({seed_for(i) for i in ids}), len(ids))

    def test_reseeding_erases_what_an_earlier_seed_pinned(self):
        random.seed(12345)
        pinned = [random.random() for _ in range(3)]
        random.seed(12345)
        reseed("tests.test_thing.Case.test_one")
        self.assertNotEqual([random.random() for _ in range(3)], pinned)

    def test_reseeding_puts_the_same_test_back_where_it_started(self):
        reseed("tests.test_thing.Case.test_one")
        first = [random.random() for _ in range(3)]
        random.seed(999)
        reseed("tests.test_thing.Case.test_one")
        self.assertEqual([random.random() for _ in range(3)], first)

    def test_numpys_global_generator_is_reseeded_too(self):
        """`audio_handlers.quantize` draws from `np.random.random_sample` when
        no generator is passed, so numpy's legacy global is the other half of
        what #430 fixed — reseeding `random` alone closes a lookalike."""
        np.random.seed(12345)
        pinned = np.random.random_sample(3).tolist()
        np.random.seed(12345)
        reseed("tests.test_thing.Case.test_one")
        self.assertNotEqual(np.random.random_sample(3).tolist(), pinned)

    def test_numpy_is_skipped_when_it_is_not_loaded(self):
        """Nothing can have touched a generator that was never imported, and
        importing numpy from the guard would load it at interpreter startup,
        before coverage.py's tracer exists."""
        random.seed(12345)
        with patch.dict(sys.modules, {"numpy": None}):
            reseed("tests.test_thing.Case.test_one")
        drawn = [random.random() for _ in range(3)]
        reseed("tests.test_thing.Case.test_one")
        self.assertEqual([random.random() for _ in range(3)], drawn, "the random half still ran")


class ArmedTest(unittest.TestCase):
    """The guard is only worth anything if every test in the run is wearing
    it. Driving a leaking TestCase through the real machinery is what says so
    — an assertion that `unittest.TestCase.run` is patched would pass against
    a patch that reseeds nothing."""

    def test_the_suite_runs_with_the_guard_installed(self):
        self.assertTrue(_rng_sandbox._armed)

    def test_this_test_started_from_its_own_seed(self):
        """Against the live process-wide state as the wrapper left it, not
        against another call to `reseed` — comparing the function with itself
        would pass with nothing armed at all."""
        self.assertEqual(random.getstate(), random.Random(seed_for(self.id())).getstate())

    def test_a_seed_one_test_leaves_behind_does_not_reach_the_next(self):
        drawn = []

        class Leaky(unittest.TestCase):
            def runTest(self):
                random.seed(4321)

        class Draws(unittest.TestCase):
            def runTest(self):
                drawn.append(random.random())

        result = unittest.TestResult()
        Draws().run(result)
        Leaky().run(result)
        Draws().run(result)

        self.assertEqual(result.errors, [], result.errors)
        self.assertEqual(drawn[0], drawn[1], "the second run drew from the leaked seed")
