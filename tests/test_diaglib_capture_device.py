"""The diag tools' capture device: the default is the Cam Link by USB identity,
and a missing Cam Link stops the tool rather than opening another camera."""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import cv2

from c64cast.control import camera
from c64cast.control.camera import CameraInfo

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


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

_AVF = int(cv2.CAP_AVFOUNDATION)
_FACETIME = CameraInfo(index=0, name="FaceTime HD Camera", vid=None, pid=None, backend=_AVF)
_CAMLINK = CameraInfo(index=1, name="Cam Link 4K", vid=0x0FD9, pid=0x0066, backend=_AVF)


class CaptureDeviceTestCase(unittest.TestCase):
    """Runs each test against a faked camera list and with neither diag camera
    env var set, whatever the shell running the suite exports."""

    cameras: list[CameraInfo] = []

    def setUp(self) -> None:
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("C64_DIAG_CAMERA", None)
        os.environ.pop("C64_DIAG_CV2", None)
        for name, value in (
            ("camera_enumeration_available", lambda: True),
            ("enumerate_cameras", lambda: list(self.cameras)),
        ):
            p = patch.object(camera, name, value)
            p.start()
            self.addCleanup(p.stop)


class CamLinkPresentTest(CaptureDeviceTestCase):
    cameras = [_FACETIME, _CAMLINK]

    def test_default_resolves_to_the_cam_link(self) -> None:
        self.assertEqual(_diaglib.resolve_capture(None), (1, _AVF))

    def test_explicit_index_name_and_vidpid(self) -> None:
        self.assertEqual(_diaglib.resolve_capture(0), (0, None))
        self.assertEqual(_diaglib.resolve_capture("0"), (0, None))
        self.assertEqual(_diaglib.resolve_capture("FaceTime"), (0, _AVF))
        self.assertEqual(_diaglib.resolve_capture("cam link"), (1, _AVF))
        self.assertEqual(_diaglib.resolve_capture("0fd9:0066"), (1, _AVF))

    def test_camera_env_sets_the_default(self) -> None:
        os.environ["C64_DIAG_CAMERA"] = "FaceTime"
        self.assertEqual(_diaglib.resolve_capture(None), (0, _AVF))

    def test_legacy_index_env_still_works_and_warns(self) -> None:
        os.environ["C64_DIAG_CV2"] = "0"
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(_diaglib.resolve_capture(None), (0, None))
        self.assertIn("C64_DIAG_CV2=0", err.getvalue())
        self.assertIn("C64_DIAG_CAMERA=0fd9:0066", err.getvalue())

    def test_legacy_env_that_is_not_an_index_is_refused(self) -> None:
        os.environ["C64_DIAG_CV2"] = "Cam Link"
        with self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_capture(None)
        self.assertIn("C64_DIAG_CAMERA", str(cm.exception))

    def test_open_capture_opens_the_resolved_index_on_its_backend(self) -> None:
        with patch.object(cv2, "VideoCapture") as video_capture:
            video_capture.return_value.isOpened.return_value = True
            cap = _diaglib.open_capture(None)
        video_capture.assert_called_once_with(1, _AVF)
        self.assertIs(cap, video_capture.return_value)


class CamLinkAbsentTest(CaptureDeviceTestCase):
    """The incident: the Cam Link fell off the bus and FaceTime became index 0."""

    cameras = [_FACETIME]

    def test_default_refuses_rather_than_opening_facetime(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_capture(None)
        message = str(cm.exception)
        self.assertIn("Cam Link 4K (USB 0fd9:0066) is not connected", message)
        self.assertIn("FaceTime HD Camera", message)

    def test_open_capture_opens_no_camera(self) -> None:
        with patch.object(cv2, "VideoCapture") as video_capture:
            with self.assertRaises(SystemExit):
                _diaglib.open_capture(None)
        video_capture.assert_not_called()

    def test_explicit_vidpid_also_refuses(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_capture("0fd9:0066")
        self.assertIn("not connected", str(cm.exception))

    def test_an_explicit_index_is_still_honored(self) -> None:
        self.assertEqual(_diaglib.resolve_capture("0"), (0, None))

    def test_default_refuses_without_the_camera_extra(self) -> None:
        with patch.object(camera, "camera_enumeration_available", lambda: False):
            with self.assertRaises(SystemExit) as cm:
                _diaglib.resolve_capture(None)
        self.assertIn("camera' extra", str(cm.exception))
        self.assertNotIn("not connected", str(cm.exception))


class CaptureDeviceArgTest(unittest.TestCase):
    def _parser(self, *aliases: str) -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser()
        _diaglib.add_capture_device_arg(ap, *aliases)
        return ap

    def test_no_flag_leaves_the_default_to_open_capture(self) -> None:
        self.assertIsNone(self._parser("-d").parse_args([]).device)

    def test_old_flag_names_are_aliases(self) -> None:
        for flag in ("--index", "--cv2-index", "--cam", "-d"):
            with self.subTest(flag=flag):
                args = self._parser(flag).parse_args([flag, "2"])
                self.assertEqual(args.device, "2")
        args = self._parser().parse_args(["--device", "0fd9:0066"])
        self.assertEqual(args.device, "0fd9:0066")


if __name__ == "__main__":
    unittest.main()
