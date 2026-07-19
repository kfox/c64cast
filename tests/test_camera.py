"""Camera name / USB VID:PID device selection (no hardware; enumerate_cameras
patched with fake CameraInfo objects, so these run without the `camera` extra)."""

import unittest
from unittest import mock

from c64cast import camera
from c64cast.camera import CameraInfo
from c64cast.config import ConfigError


def _cam(index, name, vid=None, pid=None, backend=1200):
    return CameraInfo(index=index, name=name, vid=vid, pid=pid, backend=backend)


# A representative macOS enumeration: a built-in cam (no USB IDs), an Elgato
# Cam Link 4K, and a virtual camera.
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
            mock.patch("c64cast.camera.enumerate_cameras", return_value=cams),
            mock.patch("c64cast.camera.camera_enumeration_available", return_value=available),
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
            with self.assertLogs("c64cast.camera", level="WARNING"):
                self.assertEqual(camera.resolve_camera_index("Cam Link"), (1, 1200))


class CameraInfoTest(unittest.TestCase):
    def test_vidpid_str_padded_lowercase(self):
        self.assertEqual(CAMLINK.vidpid_str(), "0fd9:0066")

    def test_vidpid_str_none_when_missing(self):
        self.assertIsNone(FACETIME.vidpid_str())


if __name__ == "__main__":
    unittest.main()
