"""The NMI diag probes arm and pace at the latch the streamer arms.

A probe that rounds its own latch measures a different rate from production's
wherever the streamer clamps: past the handler budget and below the 16-bit
floor.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import patch

from _fakes import FakeAPI

from c64cast.audio.audio import AudioStreamer
from c64cast.hw.api import Ultimate64API

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"

# In range, past the NTSC and PAL handler ceilings, and below the latch floor.
_RATES = (8000, 12000, 14000, 20000, 44100, 15)


def _load(name: str) -> ModuleType:
    """scripts/diags/ is not a package; load a tool by path without leaving it,
    its sibling tools, or the sys.path entry its ``import _diaglib`` needs,
    behind in the worker. Package modules it imports stay loaded: unloading
    cv2's submodules breaks a later cv2 import."""
    local = {p.stem for p in _DIAGS.glob("*.py")} - set(sys.modules)
    try:
        with patch.object(sys, "path", [str(_DIAGS), *sys.path]):
            spec = importlib.util.spec_from_file_location(name, _DIAGS / f"{name}.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
    finally:
        for stem in local:
            sys.modules.pop(stem, None)


def _sounddevice_importable() -> bool:
    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


class ProbeLatchTest(unittest.TestCase):
    def _assert_paces_like_the_streamer(self, module_name: str) -> None:
        probe = _load(module_name)
        for system in ("NTSC", "PAL"):
            for rate in _RATES:
                with self.subTest(system=system, rate=rate):
                    streamer = AudioStreamer(cast(Ultimate64API, FakeAPI()), rate, system)
                    self.assertEqual(probe.effective_rate(rate, system), streamer.effective_rate)

    def test_the_ring_race_probe_paces_at_the_streamers_effective_rate(self) -> None:
        self._assert_paces_like_the_streamer("ring_race_probe")

    @unittest.skipUnless(_sounddevice_importable(), "audio_fm_probe needs sounddevice")
    def test_the_audio_fm_probe_paces_at_the_streamers_effective_rate(self) -> None:
        self._assert_paces_like_the_streamer("audio_fm_probe")


if __name__ == "__main__":
    unittest.main()
