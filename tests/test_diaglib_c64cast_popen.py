"""The diag tools start c64cast on their own checkout's code, whatever
directory they are run from."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
_DIAGS = _REPO / "scripts" / "diags"


def _load_diaglib() -> ModuleType:
    """scripts/diags/ is not a package; load _diaglib by path without leaving
    it, or its sys.path insert, behind in the worker."""
    with patch.object(sys, "path", list(sys.path)), patch.dict(sys.modules):
        spec = importlib.util.spec_from_file_location("_diaglib", _DIAGS / "_diaglib.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["_diaglib"] = module
        spec.loader.exec_module(module)
        return module


_diaglib = _load_diaglib()


def _decoy_checkout() -> Path:
    root = Path(tempfile.mkdtemp())
    package = root / "c64cast"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text("print('decoy checkout')\n")
    return root


def _version_output(**popen_kwargs) -> str:
    app = _diaglib.c64cast_popen(
        ["--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kwargs
    )
    try:
        out, _ = app.communicate(timeout=20)
    finally:
        app.kill()
        app.wait()
    return out.decode()


class C64castPopenTests(unittest.TestCase):
    def test_another_checkouts_c64cast_in_the_cwd_is_not_the_one_run(self):
        out = _version_output(cwd=_decoy_checkout())
        self.assertIn(f"({_REPO})", out)

    def test_another_checkout_ahead_of_the_install_is_not_the_one_run(self):
        env = {**os.environ, "PYTHONPATH": str(_decoy_checkout())}
        out = _version_output(env=env)
        self.assertIn(f"({_REPO})", out)


if __name__ == "__main__":
    unittest.main()
