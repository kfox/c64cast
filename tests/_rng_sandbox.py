"""Start every test from an RNG state no earlier test can have chosen.

`random` and `numpy.random`'s legacy global generator are both process-wide,
and this program draws from both: eight `c64cast` modules import `random` for
scene, background and pool picks, and `audio_handlers.quantize` reaches for
`np.random.random_sample` when no generator is passed. `make test` runs
`unittest_parallel`, so a test that seeds either one pins the sequence every
later test in that worker — and the production code inside it — will draw
from. Which tests share a worker changes per run, so a failure caused that way
lands on a different test each time.

#430 was that leak, and eb903e8 closed the instance by hand: one class's
`setUp` snapshotting `random.getstate()` and restoring it from `addCleanup`,
plus one test swapped off `np.random.randint`. The next module to call
`random.seed()` re-introduces it, which is what this closes.

:func:`arm` wraps `unittest.TestCase.run` to seed both generators before the
test body, so whatever the previous test left behind is gone before this one
can read it. A test that wants a particular sequence still calls
`random.seed()` itself, inside the test or in `setUp`, and gets it.

Why seeding rather than comparing `random.getstate()` before and after: a test
that merely *consumes* draws also changes that state and is not leaking
anything, so the comparison needs a per-test exemption list — and a list
somebody has to keep is what `_fs_sandbox` exists to stop being the answer.

Why the seed is derived from the test id rather than from a per-worker
generator: a seed handed out by a meta-generator depends on how many tests
drew from it first, which is the same order dependence in a new place. Keyed
on the id, a test starts from the same state whether it runs first in a
worker, last, alone under `make test T=…`, or on another machine — so a
failure that turns on the RNG reproduces.

Why `blake2b` rather than `hash()`: `hash()` of a `str` is salted per process
by `PYTHONHASHSEED`, so every worker and every run would give the same test a
different state, which is the property being removed.

numpy is seeded only when it is already imported. Nothing can have touched its
global generator before then, and importing it from here would put numpy into
`sys.modules` at interpreter startup — the trap `_fs_sandbox`'s
`neutralize_local_chargen` documents, where a module imported before
coverage.py's tracer exists reports a fraction of what it runs.

Blind spots worth knowing:

* A `random.Random()` instance and a `np.random.Generator` are not
  process-wide and are not touched. They are the shape to prefer anyway.
* A seed set in `setUpClass`, in `setUpModule` or at module import is
  overwritten before the first test under it runs, since the reseed happens
  per test. Seed inside the test or in `setUp`.
"""

from __future__ import annotations

import hashlib
import random
import sys
import unittest
from typing import Any

_armed = False

#: `np.random.seed` takes a 32-bit value; `random.seed` takes the whole 64.
_NUMPY_SEED_MODULUS = 2**32


def seed_for(test_id: str) -> int:
    """The seed the test named `test_id` starts from."""
    return int.from_bytes(hashlib.blake2b(test_id.encode("utf-8"), digest_size=8).digest(), "big")


def reseed(test_id: str) -> None:
    """Point both process-wide generators at `test_id`'s own seed.

    Exposed for tests/test_rng_sandbox.py, which drives it directly rather
    than leaking a seed to watch the armed hook clear it.
    """
    seed = seed_for(test_id)
    random.seed(seed)
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        numpy.random.seed(seed % _NUMPY_SEED_MODULUS)


def arm() -> None:
    """Install the per-test reseed. Idempotent."""
    global _armed
    if _armed:
        return
    _armed = True
    original = unittest.TestCase.run

    def run(self: unittest.TestCase, result: Any = None) -> Any:
        reseed(self.id())
        return original(self, result)

    unittest.TestCase.run = run  # type: ignore[method-assign]
