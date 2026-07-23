"""Audio input device name-substring selection: resolve_audio_input_device
(no hardware; c64cast.audio.sd patched with a fake whose query_devices() returns
a device list, so these run without the 'mic' extra).

PortAudio exposes no USB VID:PID, so — unlike the camera resolver — the only
string form is a name substring, and the never-raises contract falls back to the
system default (index -1) with a warning on miss / no sounddevice."""

from __future__ import annotations

import unittest
from typing import Any

from c64cast import audio as audio_mod
from c64cast.audio import resolve_audio_input_device


class _FakeSD:
    """Minimal stand-in for sounddevice: query_devices() with no args returns the
    full device list (real DeviceList semantics), matching what the resolver and
    --list-devices iterate."""

    def __init__(self, devices: list[dict[str, Any]]):
        self._devices = devices

    def query_devices(self, idx: Any = None, kind: Any = None) -> Any:
        if idx is None:
            return list(self._devices)
        return self._devices[idx]


# A representative macOS enumeration: default mic, an Elgato Cam Link 4K input,
# and an output-only device that must never match.
DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    {"name": "Cam Link 4K", "max_input_channels": 2},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0},
]


class ResolveAudioInputDeviceTest(unittest.TestCase):
    def _patch(self, devices: list[dict[str, Any]], available: bool = True):
        orig_sd, orig_avail = audio_mod.sd, audio_mod.AUDIO_AVAILABLE
        audio_mod.sd = _FakeSD(devices) if available else None
        audio_mod.AUDIO_AVAILABLE = available
        self.addCleanup(lambda: setattr(audio_mod, "sd", orig_sd))
        self.addCleanup(lambda: setattr(audio_mod, "AUDIO_AVAILABLE", orig_avail))

    def test_int_passthrough(self):
        self.assertEqual(resolve_audio_input_device(2), 2)

    def test_negative_int_passthrough(self):
        # -1 stays -1 ("system default"); the caller maps it to PortAudio None.
        self.assertEqual(resolve_audio_input_device(-1), -1)

    def test_int_in_a_string(self):
        self.assertEqual(resolve_audio_input_device("2"), 2)

    def test_empty_string_is_default(self):
        self.assertEqual(resolve_audio_input_device("   "), -1)

    def test_name_substring_match(self):
        self._patch(DEVICES)
        self.assertEqual(resolve_audio_input_device("cam link"), 1)

    def test_output_only_device_never_matches(self):
        self._patch(DEVICES)
        with self.assertLogs("c64cast.audio", level="WARNING"):
            self.assertEqual(resolve_audio_input_device("Speakers"), -1)

    def test_no_match_warns_and_defaults(self):
        self._patch(DEVICES)
        with self.assertLogs("c64cast.audio", level="WARNING"):
            self.assertEqual(resolve_audio_input_device("Scarlett"), -1)

    def test_multiple_matches_warns_and_picks_first(self):
        devices = [
            {"name": "Cam Link 4K", "max_input_channels": 2},
            {"name": "Cam Link 4K #2", "max_input_channels": 2},
        ]
        self._patch(devices)
        with self.assertLogs("c64cast.audio", level="WARNING"):
            self.assertEqual(resolve_audio_input_device("Cam Link"), 0)

    def test_name_without_sounddevice_warns_and_defaults(self):
        self._patch(DEVICES, available=False)
        with self.assertLogs("c64cast.audio", level="WARNING"):
            self.assertEqual(resolve_audio_input_device("Cam Link"), -1)


if __name__ == "__main__":
    unittest.main()
