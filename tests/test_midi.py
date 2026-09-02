"""Direct contract tests for c64cast._midi.open_input_port — the shared
port resolver behind MidiScene, AsidScene and MidiControlListener's own
`_open_port`. Exercised with `mido` mocked out, so no real MIDI hardware (or
even the `midi` extra) is needed."""

from __future__ import annotations

import unittest
from unittest import mock

from c64cast import _midi


class _FakePort:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_mido(names: list[str], opened: list[str]):
    fake = mock.MagicMock()
    fake.get_input_names.return_value = names
    fake.open_input.side_effect = lambda n: opened.append(n) or _FakePort()
    return mock.patch.object(_midi, "mido", fake)


class OpenInputPortTest(unittest.TestCase):
    def test_default_spec_opens_the_first_port(self):
        opened: list[str] = []
        with _patch_mido(["Port A", "Port B"], opened):
            port, name = _midi.open_input_port(None, label="test")
        self.assertEqual(name, "Port A")
        self.assertEqual(opened, ["Port A"])

    def test_empty_and_default_string_also_pick_the_first_port(self):
        for spec in ("", "default"):
            with self.subTest(spec=spec):
                opened: list[str] = []
                with _patch_mido(["Port A"], opened):
                    _midi.open_input_port(spec, label="test")
                self.assertEqual(opened, ["Port A"])

    def test_no_ports_available_raises_with_the_caller_label(self):
        with _patch_mido([], []):
            with self.assertRaisesRegex(RuntimeError, "test: no MIDI input ports"):
                _midi.open_input_port(None, label="test")

    def test_substring_match_is_case_insensitive(self):
        opened: list[str] = []
        with _patch_mido(["IAC Bus 1", "Launch Control XL"], opened):
            _midi.open_input_port("launch", label="test")
        self.assertEqual(opened, ["Launch Control XL"])

    def test_no_match_raises_naming_the_spec_and_available_ports(self):
        with _patch_mido(["IAC Bus 1"], []):
            with self.assertRaisesRegex(RuntimeError, "nonexistent.*IAC Bus 1"):
                _midi.open_input_port("nonexistent", label="test")

    def test_missing_midi_extra_raises_a_named_runtime_error_not_an_assert(self):
        # Regression: this used to be a bare `assert mido is not None`, which
        # `python -O` strips and which otherwise surfaces as an unhelpful
        # AttributeError on None, naming nothing about the missing extra.
        with mock.patch.object(_midi, "MIDI_AVAILABLE", False):
            with self.assertRaisesRegex(RuntimeError, "midi.*extra"):
                _midi.open_input_port(None, label="test")


if __name__ == "__main__":
    unittest.main()
