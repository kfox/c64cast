"""Camera name / USB VID:PID device selection (no hardware; enumerate_cameras
patched with fake CameraInfo objects, so these run without the `camera` extra)."""

import unittest
from unittest import mock

from c64cast.app.config import ConfigError
from c64cast.control import camera
from c64cast.control.camera import CameraInfo


def _cam(index, name, vid=None, pid=None, backend=1200):
    return CameraInfo(index=index, name=name, vid=vid, pid=pid, backend=backend)


# A representative macOS enumeration; the built-in cam reports no USB IDs.
FACETIME = _cam(0, "FaceTime HD Camera")
CAMLINK = _cam(1, "Cam Link 4K", vid=0x0FD9, pid=0x0066)
OBSVIRT = _cam(2, "OBS Virtual Camera")


class ParseCameraDeviceTest(unittest.TestCase):
    """Offline syntax validation — no enumeration."""

    def test_int_ok(self):
        camera.parse_camera_device(0, field_name="[video].device")
        camera.parse_camera_device(-1, field_name="[video].device")

    def test_name_substring_ok(self):
        camera.parse_camera_device("Cam Link", field_name="[video].device")

    def test_valid_vidpid_ok(self):
        camera.parse_camera_device("0fd9:0066", field_name="[video].device")

    def test_int_in_a_string_ok(self):
        camera.parse_camera_device("0", field_name="[video].device")

    def test_malformed_vidpid_raises(self):
        with self.assertRaises(ConfigError) as cm:
            camera.parse_camera_device("0fzz:0066", field_name="[video].device")
        self.assertIn("[video].device", str(cm.exception))
        self.assertIn("VID:PID", str(cm.exception))

    def test_empty_string_raises(self):
        with self.assertRaises(ConfigError):
            camera.parse_camera_device("   ", field_name="[video].device")


class ResolveCameraIndexTest(unittest.TestCase):
    def _patch(self, cams, available=True):
        # Patch the isolated wrappers so tests don't need the extra installed.
        return (
            mock.patch("c64cast.control.camera.enumerate_cameras", return_value=cams),
            mock.patch(
                "c64cast.control.camera.camera_enumeration_available", return_value=available
            ),
        )

    def test_int_passthrough_backend_none(self):
        self.assertEqual(camera.resolve_camera_index(1), (1, None))

    def test_negative_int_maps_to_zero(self):
        self.assertEqual(camera.resolve_camera_index(-1), (0, None))

    def test_int_in_a_string_passthrough(self):
        self.assertEqual(camera.resolve_camera_index("3"), (3, None))

    def test_name_substring_match(self):
        enum_p, avail_p = self._patch([FACETIME, CAMLINK, OBSVIRT])
        with enum_p, avail_p:
            self.assertEqual(camera.resolve_camera_index("cam link"), (1, 1200))

    def test_vidpid_match(self):
        enum_p, avail_p = self._patch([FACETIME, CAMLINK, OBSVIRT])
        with enum_p, avail_p:
            self.assertEqual(camera.resolve_camera_index("0fd9:0066"), (1, 1200))

    def test_no_match_raises_with_available_list(self):
        enum_p, avail_p = self._patch([FACETIME, OBSVIRT])
        with enum_p, avail_p, self.assertRaises(RuntimeError) as cm:
            camera.resolve_camera_index("Cam Link")
        self.assertIn("no camera matched", str(cm.exception))

    def test_missing_extra_raises_actionable(self):
        enum_p, avail_p = self._patch([], available=False)
        with enum_p, avail_p, self.assertRaises(RuntimeError) as cm:
            camera.resolve_camera_index("Cam Link")
        self.assertIn("camera", str(cm.exception).lower())

    def test_multiple_matches_warns_and_picks_first(self):
        dup = _cam(4, "Cam Link 4K #2", vid=0x0FD9, pid=0x0066)
        enum_p, avail_p = self._patch([CAMLINK, dup])
        with enum_p, avail_p:
            with self.assertLogs("c64cast.control.camera", level="WARNING"):
                self.assertEqual(camera.resolve_camera_index("Cam Link"), (1, 1200))


class CameraInfoTest(unittest.TestCase):
    def test_vidpid_str_padded_lowercase(self):
        self.assertEqual(CAMLINK.vidpid_str(), "0fd9:0066")

    def test_vidpid_str_none_when_missing(self):
        self.assertIsNone(FACETIME.vidpid_str())


class PickCaptureCameraTest(unittest.TestCase):
    """:func:`camera.pick_capture_camera`: the one camera the classifier
    accepts, or an error that carries the enumeration."""

    def _pick(self, cams, available=True):
        with (
            mock.patch.object(camera, "camera_enumeration_available", return_value=available),
            mock.patch.object(camera, "enumerate_cameras", return_value=list(cams)),
        ):
            return camera.pick_capture_camera()

    def test_the_one_capture_stick_is_picked(self):
        self.assertEqual(self._pick([FACETIME, CAMLINK, OBSVIRT]), CAMLINK)

    def test_no_capture_stick_raises_with_the_cameras(self):
        with self.assertRaises(camera.CaptureCameraError) as cm:
            self._pick([FACETIME, OBSVIRT])
        self.assertFalse(cm.exception.extra_missing)
        self.assertEqual(cm.exception.cameras, [FACETIME, OBSVIRT])
        self.assertIn("no connected camera", str(cm.exception))

    def test_two_capture_sticks_raise(self):
        second = _cam(3, "Game Capture HD60 S+", vid=0x0FD9, pid=0x006A)
        with self.assertRaises(camera.CaptureCameraError) as cm:
            self._pick([CAMLINK, second])
        self.assertIn("2 connected cameras", str(cm.exception))

    # cv2-enumerate-cameras under CAP_ANY on Linux: every camera once per
    # backend (GStreamer 1800, V4L2 200) at backend + N, reported as CAP_ANY.
    @staticmethod
    def _linux(n, name, vid=None, pid=None):
        return [_cam(backend + n, name, vid, pid, backend=0) for backend in (1800, 200)]

    def test_a_linux_stick_listed_once_per_backend_is_one_camera(self):
        cams = [*self._linux(0, "Integrated Camera"), *self._linux(2, "Cam Link 4K", 0x0FD9, 0x66)]
        self.assertEqual(self._pick(cams).name, "Cam Link 4K")

    def test_two_identical_linux_sticks_stay_two(self):
        cams = [
            *self._linux(2, "Cam Link 4K", 0x0FD9, 0x66),
            *self._linux(4, "Cam Link 4K", 0x0FD9, 0x66),
        ]
        with self.assertRaises(camera.CaptureCameraError) as cm:
            self._pick(cams)
        self.assertIn("2 connected cameras", str(cm.exception))

    def test_a_missing_extra_says_so(self):
        with self.assertRaises(camera.CaptureCameraError) as cm:
            self._pick([CAMLINK], available=False)
        self.assertTrue(cm.exception.extra_missing)
        self.assertIn("'camera' extra", str(cm.exception))


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
                self.assertFalse(camera.looks_like_hdmi_capture(name, "1234:5678"))

    def test_every_exclusion_alone_vetoes_a_known_device(self) -> None:
        """Each pattern is the only one some name above matches, so dropping
        any pattern lets that device through."""
        patterns = camera.NOT_CAPTURE_NAME_PATTERNS
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
                self.assertFalse(camera.looks_like_hdmi_capture("Cam Link 4K", usb_id))

    def test_a_device_with_no_name_is_not_picked(self) -> None:
        for name in ("", "   "):
            with self.subTest(name=name):
                self.assertFalse(camera.looks_like_hdmi_capture(name, "1234:5678"))

    def test_capture_sticks_are_picked(self) -> None:
        for name in ("Cam Link 4K", "Game Capture HD60 S+", "USB Video", "Live Gamer Ultra"):
            with self.subTest(name=name):
                self.assertTrue(camera.looks_like_hdmi_capture(name, "1234:5678"))


if __name__ == "__main__":
    unittest.main()
