"""The diag tools' audio input: named by -D or the environment, else the one
input named like the capture camera, and never the system default input."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import cv2

from c64cast.control import camera
from c64cast.control.camera import CameraInfo

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


def _load_diaglib() -> ModuleType:
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

_MIC = "MacBook Pro Microphone"


class _FakeSoundDevice:
    """The part of sounddevice the resolver may touch. Reading ``default``
    fails the test: the system default input is never a candidate."""

    def __init__(self, devices: Sequence[tuple[str, int] | tuple[str, int, int]]) -> None:
        self._devices = [
            {"name": n, "max_input_channels": ch, "hostapi": sum(api)} for n, ch, *api in devices
        ]

    def query_devices(self):
        return list(self._devices)

    @property
    def default(self):
        raise AssertionError("the resolver read the system default device")


def _ffmpeg_listing(video: list[str], audio: list[str]) -> str:
    lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:"]
    lines += [f"[AVFoundation indev @ 0x1] [{i}] {n}" for i, n in enumerate(video)]
    lines.append("[AVFoundation indev @ 0x1] AVFoundation audio devices:")
    lines += [f"[AVFoundation indev @ 0x1] [{i}] {n}" for i, n in enumerate(audio)]
    return "\n".join(lines)


class AudioDeviceTestCase(unittest.TestCase):
    """A faked camera list, audio inputs on both backends, and none of the
    diag device variables set."""

    cameras: list[CameraInfo] = [_FACETIME, _CAMLINK]
    sd_inputs: list[tuple[str, int] | tuple[str, int, int]] = [
        (_MIC, 1),
        ("Cam Link 4K", 2),
        ("Speakers", 0),
    ]
    avf_inputs: list[str] = [_MIC, "ZoomAudioDevice", "Cam Link 4K"]

    def setUp(self) -> None:
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("C64_DIAG_CAMERA", "C64_DIAG_CV2", "C64_DIAG_SD_AUDIO", "C64_DIAG_AVF_AUDIO"):
            os.environ.pop(var, None)
        listing = _ffmpeg_listing([c.name for c in self.cameras], self.avf_inputs)

        def fake_run(argv, **_kwargs):
            self.assertEqual(argv[:3], ["ffmpeg", "-hide_banner", "-f"])
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=listing)

        for target, name, value in (
            (camera, "camera_enumeration_available", lambda: True),
            (camera, "enumerate_cameras", lambda: list(self.cameras)),
            (subprocess, "run", fake_run),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(sys.modules, {"sounddevice": _FakeSoundDevice(self.sd_inputs)})
        p.start()
        self.addCleanup(p.stop)

    def resolve(self, backend: str, spec=None, **kwargs):
        err = io.StringIO()
        with redirect_stderr(err):
            picked = _diaglib.resolve_audio_input(backend, spec, **kwargs)
        return picked, err.getvalue()

    def assert_refuses(self, backend: str, *inputs: str, spec=None, **kwargs) -> str:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            _diaglib.resolve_audio_input(backend, spec, **kwargs)
        message = str(cm.exception)
        for name in inputs:
            self.assertIn(name, message)
        return message


class DerivedFromTheCameraTest(AudioDeviceTestCase):
    def test_sounddevice_input_named_like_the_autopicked_stick(self) -> None:
        picked, err = self.resolve("sd")
        self.assertEqual(picked, _diaglib.AudioInput(1, "Cam Link 4K"))
        self.assertIn("[capture] auto-picked [1] Cam Link 4K", err)
        self.assertIn("[audio] picked 1 Cam Link 4K", err)

    def test_avfoundation_input_named_like_the_autopicked_stick(self) -> None:
        picked, _ = self.resolve("avf")
        self.assertEqual(picked, _diaglib.AudioInput(":2", "Cam Link 4K"))

    def test_without_the_camera_extra_nothing_is_derived(self) -> None:
        with patch.object(camera, "camera_enumeration_available", lambda: False):
            self.assert_refuses("sd", _MIC, "Cam Link 4K")

    def test_ffmpeg_that_cannot_list_refuses(self) -> None:
        """A named input is checked against ffmpeg's list too, so naming one
        is no way around an ffmpeg that cannot list, and the message says so."""
        for spec in (None, ":2"):
            with (
                self.subTest(spec=spec),
                patch.object(subprocess, "run", side_effect=OSError("no ffmpeg")),
            ):
                message = self.assert_refuses("avf", "no ffmpeg", spec=spec)
                self.assertIn("ffmpeg has to run first", message)


class TwoSticksTest(AudioDeviceTestCase):
    cameras = [_FACETIME, _CAMLINK, _HD60]
    sd_inputs = [(_MIC, 1), ("Cam Link 4K", 2), ("Game Capture HD60 S+", 2)]

    def test_the_camera_a_tool_names_decides_the_audio(self) -> None:
        self.assertEqual(self.resolve("sd", camera="HD60")[0].device, 2)
        os.environ["C64_DIAG_CAMERA"] = "cam link"
        self.assertEqual(self.resolve("sd")[0].device, 1)

    def test_an_ambiguous_camera_autopick_picks_no_audio(self) -> None:
        self.assertIn("2 connected cameras", self.assert_refuses("sd", _MIC))


class StickAudioMissingTest(AudioDeviceTestCase):
    sd_inputs = [(_MIC, 1), ("Speakers", 0)]

    def test_microphone_inputs_are_never_derived(self) -> None:
        """Only the mic and a speaker are inputs: the stick's audio is not
        there, so the tool exits listing what it found."""
        message = self.assert_refuses("sd", _MIC, "Cam Link 4K")
        self.assertIn("no audio input named like capture camera 'Cam Link 4K'", message)
        self.assertIn("-D", message)
        self.assertIn("C64_DIAG_SD_AUDIO", message)
        self.assertNotIn("Speakers", message)


class StickOffTheBusTest(AudioDeviceTestCase):
    cameras = [_FACETIME]

    def test_no_capture_camera_means_no_audio(self) -> None:
        """The stick is off the bus: the camera auto-pick refuses, and the
        audio pick refuses with it, rather than settle on the mic."""
        message = self.assert_refuses("sd", _MIC)
        self.assertIn("no connected camera looks like an HDMI capture device", message)
        self.assertIn("C64_DIAG_SD_AUDIO", message)


class TwoStickInputsTest(AudioDeviceTestCase):
    avf_inputs = [_MIC, "Cam Link 4K", "Cam Link 4K #2"]

    def test_two_inputs_named_like_the_camera_is_ambiguous(self) -> None:
        message = self.assert_refuses("avf", "Cam Link 4K #2")
        self.assertIn("more than one audio input named like capture camera", message)

    def test_an_exact_name_wins_over_a_longer_one(self) -> None:
        self.assertEqual(self.resolve("avf", "cam link 4k")[0].device, ":1")


class WindowsHostApisTest(AudioDeviceTestCase):
    """Windows lists each input once per host API; MME (0) truncates names
    to 31 characters, the others (1, 2) carry the full name."""

    sd_inputs = [
        (_MIC, 2, 0),
        ("Digital Audio Interface (Cam Li", 2, 0),
        (_MIC, 2, 1),
        ("Digital Audio Interface (Cam Link 4K)", 2, 1),
        ("Digital Audio Interface (Cam Link 4K)", 2, 2),
    ]

    def test_one_input_listed_by_several_host_apis_is_one_input(self) -> None:
        self.assertEqual(self.resolve("sd")[0].device, 3)
        self.assertEqual(self.resolve("sd", "cam link")[0].device, 3)
        self.assertEqual(self.resolve("sd", "macbook")[0].device, 0)


class WindowsTwoSticksTest(AudioDeviceTestCase):
    sd_inputs = [*WindowsHostApisTest.sd_inputs, ("Digital Audio Interface (Cam Link 4K #2)", 2, 2)]

    def test_two_inputs_in_one_host_api_stay_ambiguous(self) -> None:
        message = self.assert_refuses("sd", "Cam Link 4K #2")
        self.assertIn("more than one audio input named like capture camera", message)
        self.assertIn("more than one", self.assert_refuses("sd", spec="link 4k"))


class NamedTest(AudioDeviceTestCase):
    def test_flag_by_index_and_by_name(self) -> None:
        self.assertEqual(self.resolve("sd", "0")[0].name, _MIC)
        self.assertEqual(self.resolve("sd", 1)[0].name, "Cam Link 4K")
        self.assertEqual(self.resolve("avf", ":1")[0].name, "ZoomAudioDevice")
        self.assertEqual(self.resolve("avf", "1")[0].device, ":1")
        self.assertEqual(self.resolve("avf", "zoom")[0].device, ":1")

    def test_env_names_it_and_the_flag_wins(self) -> None:
        os.environ["C64_DIAG_SD_AUDIO"] = "macbook"
        self.assertEqual(self.resolve("sd")[0].device, 0)
        self.assertEqual(self.resolve("sd", "cam link")[0].device, 1)
        os.environ["C64_DIAG_AVF_AUDIO"] = ":2"
        self.assertEqual(self.resolve("avf")[0].name, "Cam Link 4K")

    def test_a_name_that_matches_nothing_or_several_refuses(self) -> None:
        self.assertIn("matches no audio input", self.assert_refuses("sd", _MIC, spec="yeti"))
        message = self.assert_refuses("avf", spec="o")
        self.assertIn("matches more than one audio input", message)

    def test_an_index_that_is_not_an_input_refuses(self) -> None:
        self.assert_refuses("sd", _MIC, spec="2")  # "Speakers" has no input channel
        self.assert_refuses("avf", _MIC, spec=":9")


class RefindAfterReenumerationTest(AudioDeviceTestCase):
    """The sounddevice tools find their input again once a reset has made
    PortAudio re-enumerate, by its exact name and never a longer one."""

    def refind(self, inputs: list[tuple[str, int]], audio):
        with patch.dict(sys.modules, {"sounddevice": _FakeSoundDevice(inputs)}):
            return _diaglib.refind_sd_audio_input(audio)

    def test_the_same_name_at_a_new_index(self) -> None:
        audio = _diaglib.AudioInput(1, "Cam Link 4K")
        self.assertEqual(self.refind([(_MIC, 1), ("Speakers", 0), ("Cam Link 4K", 2)], audio), 2)

    def test_a_longer_name_is_not_the_input_that_went_away(self) -> None:
        """The stick has not come back yet; its sibling is another device."""
        audio = _diaglib.AudioInput(1, "Cam Link 4K")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.refind([(_MIC, 1), ("Cam Link 4K #2", 2)], audio)
        self.assertIn("'Cam Link 4K' is gone", str(cm.exception))
        self.assertIn("Cam Link 4K #2", str(cm.exception))

    def test_a_shared_name_keeps_the_index_it_was_picked_at(self) -> None:
        inputs = [(_MIC, 1), ("USB Audio", 2), ("USB Audio", 2)]
        self.assertEqual(self.refind(inputs, _diaglib.AudioInput(2, "USB Audio")), 2)
        with self.assertRaises(SystemExit) as cm:
            self.refind(inputs, _diaglib.AudioInput(3, "USB Audio"))
        self.assertIn("now names more than one input", str(cm.exception))


class WebcamAutopickTest(unittest.TestCase):
    """vision_tune's default: the one camera that is not a capture stick."""

    def pick(self, cams: list[CameraInfo]):
        with (
            patch.object(camera, "camera_enumeration_available", lambda: True),
            patch.object(camera, "enumerate_cameras", lambda: list(cams)),
            redirect_stderr(io.StringIO()),
        ):
            return _diaglib.autopick_webcam()

    def test_picks_the_one_webcam(self) -> None:
        self.assertEqual(self.pick([_FACETIME, _CAMLINK]), "FaceTime HD Camera")

    def test_refuses_none_or_several(self) -> None:
        obs = CameraInfo(index=2, name="OBS Virtual Camera", vid=None, pid=None, backend=_AVF)
        for cams in ([_CAMLINK], [_FACETIME, _CAMLINK, obs], []):
            with self.subTest(cams=[c.name for c in cams]), self.assertRaises(SystemExit) as cm:
                self.pick(cams)
            self.assertIn("--device", str(cm.exception))

    def test_refuses_a_name_another_camera_also_matches(self) -> None:
        webcam = CameraInfo(index=0, name="HD", vid=None, pid=None, backend=_AVF)
        with self.assertRaises(SystemExit) as cm:
            self.pick([webcam, _HD60])
        self.assertIn("also matches another camera", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
