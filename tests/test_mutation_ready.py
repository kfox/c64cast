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
import importlib.util
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

    def _compile(self, mode):
        compileall.compile_dir(str(self.root), quiet=2, force=True, invalidation_mode=mode)
        pycs = list(self.root.rglob("__pycache__/*.pyc"))
        self.assertEqual(len(pycs), 1, "one source, one pyc")
        return pycs[0]

    def test_a_checked_hash_tree_passes(self):
        self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        self.assertEqual(check.unarmed([str(self.root)]), [])

    def test_a_timestamp_tree_is_reported(self):
        pyc = self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        self.assertEqual(check.unarmed([str(self.root)]), [pyc])

    def test_unchecked_hash_is_reported_too(self):
        # It is hash-based but skips validation entirely, so it never notices a
        # mutation either — the same silent green with a different cause. Bit 0
        # alone would call this armed.
        pyc = self._compile(py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.assertEqual(check.unarmed([str(self.root)]), [pyc])

    def test_a_pyc_for_another_interpreter_is_ignored(self):
        # A checkout run under more than one Python minor accumulates these and
        # no import here will ever read one. Failing on them would make the
        # check noise, which is how a check gets deleted.
        pyc = self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        other = pyc.with_name("mod.cpython-001.pyc")
        pyc.rename(other)
        self.assertEqual(check.unarmed([str(self.root)]), [])

    def test_an_orphaned_pyc_is_ignored(self):
        # Same reasoning, other cause: the source moved or was renamed, so
        # nothing imports this either.
        self._compile(py_compile.PycInvalidationMode.TIMESTAMP)
        (self.root / "mod.py").unlink()
        self.assertEqual(check.unarmed([str(self.root)]), [])

    def test_a_truncated_pyc_raises_rather_than_passing_quietly(self):
        pyc = self._compile(py_compile.PycInvalidationMode.CHECKED_HASH)
        pyc.write_bytes(pyc.read_bytes()[:4])
        with self.assertRaisesRegex(ValueError, "truncated header"):
            check.unarmed([str(self.root)])

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
        self.assertEqual(check.unarmed([str(self.root)]), [pyc])


if __name__ == "__main__":
    unittest.main()
