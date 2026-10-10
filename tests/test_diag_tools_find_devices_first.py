"""The diag tools that boot c64cast find their capture device first, so a
missing or ambiguous device stops the tool before the machine is touched."""

from __future__ import annotations

import contextlib
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
    resolved, leaving neither the sys.path entry nor the modules behind.

    ``sounddevice`` is a stand-in module: the suite runs without the ``mic``
    extra, and no tool under test reaches it before it exits."""
    with patch.object(sys, "path", [str(_DIAGS), *sys.path]), patch.dict(sys.modules):
        sys.modules["sounddevice"] = ModuleType("sounddevice")
        spec = importlib.util.spec_from_file_location(name, _DIAGS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # @dataclass looks its module up there
        spec.loader.exec_module(module)
        return module


def _temp_file(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


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
        self.assertEqual(
            self.run_tool("run_and_capture", "--config", _temp_file(".toml"), "--no-audio"),
            ["resolve"],
        )

    def test_doublebuffer_tear_ab(self) -> None:
        self.assertEqual(self.run_tool("doublebuffer_tear_ab"), ["resolve"])

    def test_flicker_tear_ab(self) -> None:
        self.assertEqual(self.run_tool("flicker_tear_ab"), ["resolve"])

    def test_menu_inject(self) -> None:
        self.assertEqual(self.run_tool("menu_inject", "--frames"), ["resolve"])

    def test_flicker_score_grid(self) -> None:
        self.assertEqual(self.run_tool("flicker_score_grid"), ["resolve"])


class FindAudioBeforeBootTest(unittest.TestCase):
    """Each audio tool runs with an audio input that resolves to nothing. It
    must exit on that before it reaches the machine or starts a recording."""

    def run_tool(self, name: str, *argv: str) -> list[str]:
        tool = _load_tool(name)
        d = tool.d
        calls: list[str] = []

        def no_audio(backend, spec=None, **_kwargs):
            calls.append("audio")
            raise SystemExit("no audio input")

        def boot(*_a, **_k):
            calls.append("boot")
            raise _Booted

        boot_paths = [
            patch.object(d, "resolve_capture", side_effect=boot),
            patch.object(d, "machine_reset", side_effect=boot),
            patch.object(d, "rest_reset", side_effect=boot),
            patch.object(d, "rest_get_config", side_effect=boot),
            patch.object(subprocess, "Popen", side_effect=boot),
            patch.object(subprocess, "run", side_effect=boot),
            patch("c64cast.hw.backend.make_backend", side_effect=boot),
        ]
        boot_paths += [
            patch.object(tool, attr, side_effect=boot)
            for attr in ("make_backend", "Ultimate64API", "build_backend")
            if hasattr(tool, attr)
        ]
        with (
            patch.object(sys, "argv", [f"{name}.py", *argv]),
            patch.object(d, "resolve_audio_input", no_audio),
            contextlib.ExitStack() as stack,
            self.assertRaises(SystemExit) as raised,
        ):
            for p in boot_paths:
                stack.enter_context(p)
            tool.main()
        self.assertEqual(str(raised.exception), "no audio input")
        return calls

    def test_every_audio_tool(self) -> None:
        clip = _temp_file(".wav")
        cfg = _temp_file(".toml")
        tools = {
            "run_and_capture": ("--config", cfg, "--frames", "0"),
            "audio_capture": (),
            "capture_fidelity_probe": (),
            "mhires_pitch_tempo_ab": (),
            "nmi_rate_sweep_ab": (),
            "nmi_pitch_ab": (),
            "tr_audio_sid_probe": ("--tcp", "127.0.0.1"),
            "reu_audio_spectrum": ("--config", cfg),
            "sampler_av_align_calib": (),
            "sampler_clock_calib": (),
            "sampler_outage_probe": (),
            "audio_fm_probe": (),
            "mahoney_dac_calib": (),
            "tr_nmi_rate_ceiling": (),
            "dsp_hw_ab": (clip,),
            "dac_curve": (),
            "nmi_rate_ab": (clip,),
            "dac_curve_playback_ab": ("--url", "u64://192.0.2.1"),
            "mahoney_slot_ring_probe": ("--url", "u64://192.0.2.1"),
        }
        for name, argv in tools.items():
            with self.subTest(tool=name):
                self.assertEqual(self.run_tool(name, *argv), ["audio"])

    def test_analyze_only_needs_no_audio_input(self) -> None:
        """``--analyze-only`` reads a WAV already on disk, so it runs with no
        audio input connected."""
        wav = _temp_file(".wav")
        for name in ("capture_fidelity_probe", "nmi_pitch_ab"):
            with self.subTest(tool=name):
                tool = _load_tool(name)
                with (
                    patch.object(sys, "argv", [f"{name}.py", "--analyze-only", wav]),
                    patch.object(tool.d, "resolve_audio_input") as resolve,
                    patch.object(tool, "analyze") as analyze,
                ):
                    self.assertEqual(tool.main(), 0)
                resolve.assert_not_called()
                analyze.assert_called_once()


if __name__ == "__main__":
    unittest.main()
