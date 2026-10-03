"""Tests for c64cast.hw.machine_input: the firmware's limits are enforced
before anything is sent, a long sequence splits into bodies the firmware
accepts, and text maps onto the matrix keys that type it."""

from __future__ import annotations

import json
import unittest

from c64cast.hw import machine_input as mi


def _events(body: bytes) -> list[dict]:
    return json.loads(body)["events"]


class ValidateEventTest(unittest.TestCase):
    def test_accepts_what_the_firmware_accepts(self):
        for event in (
            mi.keyboard_event("tap", ["left_shift", "2"]),
            mi.keyboard_event("tap", ["commodore", "restore"]),
            mi.joystick_event(2, "press", ["up", "fire"]),
            mi.joystick_event(1, "release", list(mi.JOYSTICK_INPUTS)),
            mi.RELEASE_ALL,
        ):
            with self.subTest(event=event):
                mi.validate_event(event)

    def test_refuses_what_the_firmware_refuses(self):
        for event in (
            {"kind": "mouse"},
            {"kind": "release_all", "port": 1},
            {"kind": "keyboard", "action": "tap", "inputs": ["a"]},
            {"kind": "keyboard", "transition": "tap", "inputs": ["a"], "port": 1},
            {"kind": "joystick", "transition": "tap", "inputs": ["up"]},
            mi.keyboard_event("hold", ["a"]),
            mi.keyboard_event("tap", []),
            mi.keyboard_event("tap", ["a", "a"]),
            mi.keyboard_event("tap", ["A"]),
            mi.keyboard_event("tap", [*"abcdefghi"]),
            mi.keyboard_event("press", ["restore"]),
            mi.joystick_event(3, "tap", ["up"]),
            mi.joystick_event(2, "tap", ["jump"]),
        ):
            with self.subTest(event=event), self.assertRaises(ValueError):
                mi.validate_event(event)


class EncodeBatchesTest(unittest.TestCase):
    def test_short_sequence_is_one_body_in_order(self):
        events = mi.text_to_events("LOAD")
        (body,) = mi.encode_batches(events)
        self.assertEqual(_events(body), events)

    def test_65_events_split_at_64(self):
        events = [mi.keyboard_event("tap", ["a"])] * 65
        bodies = mi.encode_batches(events)
        self.assertEqual([len(_events(b)) for b in bodies], [64, 1])

    def test_every_body_stays_under_4096_bytes(self):
        wide = mi.keyboard_event("tap", ["cursor_left_right", "cursor_up_down", "inst_del"])
        bodies = mi.encode_batches([wide] * 64)
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(b) < 4096 for b in bodies))
        self.assertEqual(sum(len(_events(b)) for b in bodies), 64)

    def test_one_bad_event_sends_nothing(self):
        with self.assertRaises(ValueError):
            mi.encode_batches([mi.keyboard_event("tap", ["a"]), mi.keyboard_event("tap", ["?"])])


class TextToEventsTest(unittest.TestCase):
    def test_letters_digits_and_return(self):
        self.assertEqual(
            [e["inputs"] for e in mi.text_to_events("Lo1\n")],
            [["l"], ["o"], ["1"], ["return"]],
        )

    def test_shifted_symbols_hold_left_shift(self):
        self.assertEqual(mi.text_to_events('"')[0]["inputs"], ["left_shift", "2"])
        self.assertEqual(mi.text_to_events("?")[0]["inputs"], ["left_shift", "slash"])

    def test_every_event_is_a_tap(self):
        self.assertEqual({e["transition"] for e in mi.text_to_events("RUN:?")}, {"tap"})

    def test_untypeable_character_is_refused(self):
        with self.assertRaises(ValueError):
            mi.text_to_events("{")


if __name__ == "__main__":
    unittest.main()
