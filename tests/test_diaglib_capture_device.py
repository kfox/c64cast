"""The diag tools' capture device: with none named, the one connected camera
that looks like an HDMI capture device, and a tool that cannot single one out
stops rather than opening another camera."""

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
_HD60 = CameraInfo(index=2, name="Game Capture HD60 S+", vid=0x0FD9, pid=0x006A, backend=_AVF)
_OBS = CameraInfo(index=2, name="OBS Virtual Camera", vid=None, pid=None, backend=_AVF)
_IPHONE = CameraInfo(index=2, name="Kelly’s iPhone Camera", vid=None, pid=None, backend=_AVF)
_USB_IPHONE = CameraInfo(index=2, name="iPhone", vid=0x05AC, pid=0x12A8, backend=_AVF)
_C920 = CameraInfo(index=2, name="HD Pro Webcam C920", vid=0x046D, pid=0x082D, backend=_AVF)


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

    def autopick(self) -> tuple[int, int | None]:
        """``resolve_capture(None)``, with the auto-pick's stderr line asserted."""
        err = io.StringIO()
        with redirect_stderr(err):
            picked = _diaglib.resolve_capture(None)
        self.assertIn("[capture] auto-picked", err.getvalue())
        return picked

    def assert_autopick_refuses(self) -> str:
        """``open_capture(None)`` exits without opening any camera, and its
        message lists every enumerated camera and how to choose one."""
        with patch.object(cv2, "VideoCapture") as video_capture:
            with self.assertRaises(SystemExit) as cm:
                _diaglib.open_capture(None)
        video_capture.assert_not_called()
        message = str(cm.exception)
        for cam in self.cameras:
            self.assertIn(cam.name, message)
            vidpid = cam.vidpid_str()
            if vidpid is not None:
                self.assertIn(vidpid, message)
        self.assertIn("--device", message)
        self.assertIn("C64_DIAG_CAMERA", message)
        return message


class CamLinkAndFaceTimeTest(CaptureDeviceTestCase):
    """This Mac's enumeration: the stick, and a built-in camera with no USB IDs."""

    cameras = [_FACETIME, _CAMLINK]

    def test_autopick_picks_the_capture_stick(self) -> None:
        self.assertEqual(self.autopick(), (1, _AVF))

    def test_explicit_index_name_and_vidpid(self) -> None:
        self.assertEqual(_diaglib.resolve_capture(0), (0, None))
        self.assertEqual(_diaglib.resolve_capture("0"), (0, None))
        self.assertEqual(_diaglib.resolve_capture("FaceTime"), (0, _AVF))
        self.assertEqual(_diaglib.resolve_capture("cam link"), (1, _AVF))
        self.assertEqual(_diaglib.resolve_capture("0fd9:0066"), (1, _AVF))

    def test_camera_env_overrides_the_autopick(self) -> None:
        os.environ["C64_DIAG_CAMERA"] = "FaceTime"
        self.assertEqual(_diaglib.resolve_capture(None), (0, _AVF))

    def test_an_explicit_device_overrides_the_env(self) -> None:
        os.environ["C64_DIAG_CAMERA"] = "FaceTime"
        self.assertEqual(_diaglib.resolve_capture("0fd9:0066"), (1, _AVF))

    def test_legacy_index_env_still_works_and_warns(self) -> None:
        os.environ["C64_DIAG_CV2"] = "0"
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(_diaglib.resolve_capture(None), (0, None))
        self.assertIn("C64_DIAG_CV2=0", err.getvalue())
        self.assertIn("set C64_DIAG_CAMERA", err.getvalue())

    def test_legacy_env_that_is_not_an_index_is_refused(self) -> None:
        os.environ["C64_DIAG_CV2"] = "Cam Link"
        with self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_capture(None)
        self.assertIn("C64_DIAG_CAMERA", str(cm.exception))

    def test_open_capture_opens_the_autopicked_index_on_its_backend(self) -> None:
        with patch.object(cv2, "VideoCapture") as video_capture, redirect_stderr(io.StringIO()):
            video_capture.return_value.isOpened.return_value = True
            cap = _diaglib.open_capture(None)
        video_capture.assert_called_once_with(1, _AVF)
        self.assertIs(cap, video_capture.return_value)


class FaceTimeOnlyTest(CaptureDeviceTestCase):
    """The incident: the stick fell off the bus and FaceTime became index 0."""

    cameras = [_FACETIME]

    def test_autopick_refuses_rather_than_opening_facetime(self) -> None:
        message = self.assert_autopick_refuses()
        self.assertIn("no connected camera looks like an HDMI capture device", message)
        self.assertIn("no USB VID:PID", message)

    def test_an_absent_vidpid_refuses(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_capture("0fd9:0066")
        self.assertIn("FaceTime HD Camera", str(cm.exception))

    def test_an_explicit_index_is_still_honored(self) -> None:
        self.assertEqual(_diaglib.resolve_capture("0"), (0, None))

    def test_autopick_refuses_without_the_camera_extra(self) -> None:
        with patch.object(camera, "camera_enumeration_available", lambda: False):
            with self.assertRaises(SystemExit) as cm:
                _diaglib.resolve_capture(None)
        self.assertIn("camera' extra", str(cm.exception))


class NoCamerasTest(CaptureDeviceTestCase):
    cameras: list[CameraInfo] = []

    def test_autopick_refuses_and_says_none_were_found(self) -> None:
        self.assertIn("(no cameras found)", self.assert_autopick_refuses())


class TwoCaptureDevicesTest(CaptureDeviceTestCase):
    cameras = [_FACETIME, _CAMLINK, _HD60]

    def test_autopick_refuses_as_ambiguous(self) -> None:
        message = self.assert_autopick_refuses()
        self.assertIn("2 connected cameras look like HDMI capture devices", message)


class VirtualCameraAndFaceTimeTest(CaptureDeviceTestCase):
    cameras = [_FACETIME, _OBS]

    def test_autopick_refuses(self) -> None:
        self.assert_autopick_refuses()


class PhoneCameraTest(CaptureDeviceTestCase):
    cameras = [_FACETIME, _IPHONE, _USB_IPHONE, _CAMLINK]

    def test_continuity_and_usb_iphone_are_excluded(self) -> None:
        self.assertEqual(self.autopick(), (1, _AVF))


class UsbWebcamTest(CaptureDeviceTestCase):
    cameras = [_C920, _CAMLINK]

    def test_a_usb_webcam_is_excluded(self) -> None:
        self.assertEqual(self.autopick(), (1, _AVF))


class ClassifierTest(unittest.TestCase):
    """:func:`looks_like_hdmi_capture` on its own, table-driven over its data."""

    #: Device names that are not HDMI capture devices, as they enumerate, each
    #: caught by one exclusion pattern alone.
    NOT_CAPTURE = (
        "HP HD Camera",
        "HD Pro Webcam C920",
        "Elgato Facecam",
        "Microsoft® LifeCam HD-3000",
        "Logitech BRIO",
        "Razer Kiyo",
        "Insta360 Link",
        "OBSBOT Tiny 4K",
        "iPhone",
        "iPad",
        "EpocCam",
        "DroidCam Source 3",
        "Reincubate Camo",
        "VirtualCam",
        "XSplit VCam",
        "mmhmm",
        "NVIDIA Broadcast",
        "screen-capture-recorder",
    )

    def test_known_non_capture_usb_devices_are_not_picked(self) -> None:
        for name in self.NOT_CAPTURE:
            with self.subTest(name=name):
                self.assertFalse(_diaglib.looks_like_hdmi_capture(name, "1234:5678"))

    def test_every_exclusion_alone_vetoes_a_known_device(self) -> None:
        """Each pattern is the only one some name above matches, so dropping
        any pattern lets that device through."""
        patterns = _diaglib.NOT_CAPTURE_NAME_PATTERNS
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    any(
                        [p for p in patterns if p in name.lower()] == [pattern]
                        for name in self.NOT_CAPTURE
                    )
                )

    def test_a_device_with_no_usb_identity_is_not_picked(self) -> None:
        for usb_id in (None, ""):
            with self.subTest(usb_id=usb_id):
                self.assertFalse(_diaglib.looks_like_hdmi_capture("Cam Link 4K", usb_id))

    def test_a_device_with_no_name_is_not_picked(self) -> None:
        for name in ("", "   "):
            with self.subTest(name=name):
                self.assertFalse(_diaglib.looks_like_hdmi_capture(name, "1234:5678"))

    def test_capture_sticks_are_picked(self) -> None:
        for name in ("Cam Link 4K", "Game Capture HD60 S+", "USB Video", "Live Gamer Ultra"):
            with self.subTest(name=name):
                self.assertTrue(_diaglib.looks_like_hdmi_capture(name, "1234:5678"))


class DefaultDeviceMessageTest(unittest.TestCase):
    def test_a_no_frame_error_names_the_default_device(self) -> None:
        message = _diaglib.no_frame_message(None, "for 5s")
        self.assertTrue(message.startswith("the default capture device returned"))
        self.assertNotIn("None", message)


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
