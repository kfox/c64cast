"""The diag tools that boot c64cast find their capture device first, so a
missing or ambiguous device stops the tool before the machine is touched."""

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

import cv2  # noqa: F401 — imported before the tools, so patch.dict(sys.modules) keeps them
import numpy  # noqa: F401

import c64cast.app.cli  # noqa: F401 — the patch target below must already be imported

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


def _load_tool(name: str) -> ModuleType:
    """Load scripts/diags/<name>.py by path, with its bare ``import _diaglib``
    resolved, leaving neither the sys.path entry nor the modules behind."""
    with patch.object(sys, "path", [str(_DIAGS), *sys.path]), patch.dict(sys.modules):
        spec = importlib.util.spec_from_file_location(name, _DIAGS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # @dataclass looks its module up there
        spec.loader.exec_module(module)
        return module


class _Booted(Exception):
    """Raised by the faked boot paths: the tool reached the machine."""


class FindDeviceBeforeBootTest(unittest.TestCase):
    """Each tool runs with a capture device that resolves to nothing. It must
    exit on that, with no c64cast launched, no stack built and no reset sent."""

    def run_tool(self, name: str, *argv: str) -> list[str]:
        tool = _load_tool(name)
        d = tool.d
        calls: list[str] = []

        def no_device(device):
            calls.append("resolve")
            raise SystemExit("no capture device")

        def boot(*_a, **_k):
            calls.append("boot")
            raise _Booted

        with (
            patch.object(sys, "argv", [f"{name}.py", *argv]),
            patch.object(d, "resolve_capture", no_device),
            patch.object(d, "open_capture", side_effect=boot),
            patch.object(d, "camlink_avf_audio", lambda: "faked"),
            patch.object(d, "machine_reset", side_effect=boot),
            patch.object(d, "rest_reset", side_effect=boot),
            patch.object(subprocess, "Popen", side_effect=boot),
            patch.object(subprocess, "run", side_effect=boot),
            patch("c64cast.app.cli.build_stack", side_effect=boot),
            self.assertRaises(SystemExit) as raised,
        ):
            tool.main()
        self.assertEqual(str(raised.exception), "no capture device")
        return calls

    def test_run_and_capture(self) -> None:
        fd, cfg = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        self.assertEqual(
            self.run_tool("run_and_capture", "--config", cfg, "--no-audio"), ["resolve"]
        )

    def test_doublebuffer_tear_ab(self) -> None:
        self.assertEqual(self.run_tool("doublebuffer_tear_ab"), ["resolve"])

    def test_flicker_tear_ab(self) -> None:
        self.assertEqual(self.run_tool("flicker_tear_ab"), ["resolve"])

    def test_menu_inject(self) -> None:
        self.assertEqual(self.run_tool("menu_inject", "--frames"), ["resolve"])

    def test_flicker_score_grid(self) -> None:
        self.assertEqual(self.run_tool("flicker_score_grid"), ["resolve"])


if __name__ == "__main__":
    unittest.main()
