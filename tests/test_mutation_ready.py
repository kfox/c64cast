"""Tests for the `make mutation-ready` post-check.

The target it guards is the one every mutation proof in this repo rests on, and
its failure mode is a green run rather than an error: bytecode left in timestamp
mode makes a same-second, same-length mutation invisible, so the proof reports
"the test does not pin this line" for a test that was fine. The check exists
because that state was previously assumed rather than measured, so the check's
own correctness is worth measuring too.

scripts/ is not a package, so the module is loaded by path (as
tests/test_build_site.py does).
"""

from __future__ import annotations

import compileall
import contextlib
import importlib.util
import io
import os
import py_compile
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_script("check_hash_based_pycs")


class HashBasedPycCheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "mod.py").write_text("X = 1\n", encoding="utf-8")

    def _unarmed(self):
        """Just the paths — the tests that care about the per-root source count
        or the reported mode assert on `scan`'s whole return value instead."""
        unarmed, _sources = check.scan([str(self.root)])
        return [pyc for pyc, _flags in unarmed]

    def _stderr_of_main(self, *roots):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = check.main(["prog", *(roots or (str(self.root),))])
        return code, err.getvalue()

    def _compile(self, mode):
        compileall.compile_dir(str(self.root), quiet=2, force=True, invalidation_mode=mode)
        pycs = list(self.root.rglob("__pycache__/*.pyc"))
        self.assertEqual(len(pycs), 1, "one source, one pyc")
        return pycs[0]

    def test_a_checked_hash_tree_passes(self):
        self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        self.assertEqual(self._unarmed(), [])

    def test_a_timestamp_tree_is_reported(self):
        pyc = self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        self.assertEqual(self._unarmed(), [pyc])

    def test_unchecked_hash_is_reported_too(self):
        # It is hash-based but skips validation entirely, so it never notices a
        # mutation either — the same silent green with a different cause. Bit 0
        # alone would call this armed.
        pyc = self._compile(py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.assertEqual(self._unarmed(), [pyc])

    def test_a_pyc_for_another_interpreter_is_ignored(self):
        # A checkout run under more than one Python minor accumulates these and
        # no import here will ever read one. Failing on them would make the
        # check noise, which is how a check gets deleted.
        pyc = self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        pyc.rename(pyc.with_name("mod.cpython-001.pyc"))
        self.assertEqual(check.scan([str(self.root)]), ([], {str(self.root): 1}))

    def test_an_orphaned_pyc_is_ignored(self):
        # Same reasoning, other cause: the source moved or was renamed, so
        # nothing imports this either.
        self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        (self.root / "mod.py").unlink()
        self.assertEqual(check.scan([str(self.root)]), ([], {str(self.root): 0}))

    def test_the_pyc_checked_is_the_one_an_import_here_would_read(self):
        # Two wrong answers were tried before this one, in both directions.
        # Reimplementing the cache filename took the tag as everything after
        # the first dot, so `mod.<tag>.opt-1.pyc` matched nothing and every
        # optimized build passed unarmed; accepting any `opt-N` instead
        # reported files a non-`-O` run will never import AND a non-`-O`
        # compileall will never rewrite — a permanent failure printing a remedy
        # that does nothing. `cache_from_source` is the function an import
        # itself calls, so it answers tag, optimization level and dotted names
        # at once.
        pyc = self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        self.assertEqual(Path(importlib.util.cache_from_source(str(self.root / "mod.py"))), pyc)
        self.assertEqual(self._unarmed(), [pyc])

        # An optimization level this interpreter is not running is not ours.
        other = pyc.with_name(f"mod.{sys.implementation.cache_tag}.opt-9.pyc")
        pyc.rename(other)
        self.assertEqual(self._unarmed(), [])

    def test_a_root_with_no_python_in_it_is_not_a_pass(self):
        # A mistyped or renamed root has no sources, which is a different fact
        # from having sources nobody imported. A global count hid it behind
        # whichever root did have some: `... c64cast_TYPO tests scripts`
        # exited 0.
        self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        code, err = self._stderr_of_main(str(self.root), str(self.root / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("no Python sources", err)
        self.assertIn("nope", err)

    def test_an_empty_scan_is_not_a_pass(self):
        # A wrong working directory or a renamed root would otherwise exit 0
        # silently, which is the failure this whole check exists to remove.
        # (Absence of *bytecode* is a different matter and deliberately not an
        # error — it cannot be told from "the module was never imported".)
        missing = str(self.root / "nope")
        self.assertEqual(check.scan([missing]), ([], {missing: 0}))
        code, err = self._stderr_of_main(missing)
        self.assertEqual(code, 1)
        self.assertIn("nothing was checked", err)

    def test_an_armed_tree_exits_zero(self):
        self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        self.assertEqual(check.main(["prog", str(self.root)]), 0)

    def test_the_failure_names_the_mode_it_found(self):
        # The message said "still timestamp-based" for every failure, which
        # sends the reader after mtimes on an unchecked-hash pyc — one of the
        # two cases the both-bits test was added for.
        self._compile(py_compile.PycInvalidationMode.UNCHECKED_HASH)
        code, err = self._stderr_of_main()
        self.assertEqual(code, 1)
        self.assertIn("unchecked-hash", err)
        self.assertNotIn("timestamp-based", err)

    def test_the_unreadable_sentinel_survives_the_mode_lookup(self):
        # It is negative, and `-1 & 0b11` is 3 — the armed value — so masking
        # before the lookup made the entry for it unreachable and printed a raw
        # flags word. That is the wrong-mode-in-the-message defect again, for
        # the mode added to fix it.
        self.assertEqual(check.describe(check._UNREADABLE), "could not be read")
        self.assertEqual(check.describe(0b00), "timestamp-based")

    def test_an_unreadable_pyc_fails_closed(self):
        # A file we cannot read is a file we cannot vouch for, and the scan
        # still covers the remaining roots rather than aborting on it.
        pyc = self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        pyc.chmod(0o000)
        self.addCleanup(pyc.chmod, 0o644)
        if os.access(pyc, os.R_OK):
            self.skipTest("running as a user that can read a 000 file")
        self.assertEqual(self._unarmed(), [pyc])
        code, err = self._stderr_of_main()
        self.assertEqual(code, 1)
        self.assertIn("could not be read", err)

    def test_a_truncated_pyc_raises_rather_than_passing_quietly(self):
        pyc = self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        pyc.write_bytes(pyc.read_bytes()[:4])
        with self.assertRaisesRegex(ValueError, "truncated header"):
            check.scan([str(self.root)])

    def test_the_flags_word_is_read_where_pep_552_puts_it(self):
        # The offset and endianness are the whole check, and getting either
        # wrong reads a byte of the magic number as the mode. Assert against a
        # hand-built header rather than against compileall's own output, which
        # would agree with any consistent misreading.
        pyc = self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        raw = bytearray(pyc.read_bytes())
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], 0b11)
        struct.pack_into("<I", raw, 4, 0b00)
        pyc.write_bytes(bytes(raw))
        self.assertEqual(self._unarmed(), [pyc])


if __name__ == "__main__":
    unittest.main()
