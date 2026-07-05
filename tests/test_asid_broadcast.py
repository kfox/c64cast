"""Unit tests for the ASID host/broadcaster (c64cast/asid_broadcast.py).

Two layers: the pure `frame_messages` delta encoder (no mido — asserts full
first frame, deltas after, and hard-restart re-send), and the `AsidBroadcaster`
lifecycle driven through a fake injected MIDI port so we can assert the
start/frame/stop message ordering without real hardware. The port-dependent
tests need mido to build the `sysex` Message, so they skip when it's absent.
"""

from __future__ import annotations

import unittest

from c64cast import asid, asid_broadcast
from c64cast.asid_broadcast import AsidBroadcaster, frame_messages
from c64cast.c64 import SID

_REG_COUNT = 25


def _img(**offsets: int) -> bytearray:
    """A 25-byte $D4xx image with the given offset→value overrides."""
    b = bytearray(_REG_COUNT)
    for off, val in offsets.items():
        b[int(off)] = val
    return b


class FrameMessagesTest(unittest.TestCase):
    def test_first_frame_is_full(self):
        img = _img(**{"0": 0x10, "1": 0x20, "24": 0x0F})
        msgs = frame_messages([img], [None])
        self.assertEqual(len(msgs), 1)
        u = asid.decode(msgs[0])
        assert u is not None
        # Every nonzero offset (and the zeros differing from an absent baseline)
        # is present: a None baseline sends the whole image.
        self.assertEqual(u.regs[0x00], 0x10)
        self.assertEqual(u.regs[0x01], 0x20)
        self.assertEqual(u.regs[0x18], 0x0F)
        self.assertEqual(len(u.regs), _REG_COUNT)

    def test_second_frame_is_delta_only(self):
        last = _img(**{"0": 0x10, "1": 0x20})
        cur = _img(**{"0": 0x10, "1": 0x22})  # only offset 1 changed
        msgs = frame_messages([cur], [last])
        self.assertEqual(len(msgs), 1)
        u = asid.decode(msgs[0])
        assert u is not None
        self.assertEqual(u.regs, {0x01: 0x22})

    def test_unchanged_frame_emits_no_message(self):
        img = _img(**{"0": 0x10})
        self.assertEqual(frame_messages([img], [bytearray(img)]), [])

    def test_retrigger_forces_control_even_when_unchanged(self):
        # Voice 0 control ($04) identical to last frame, but a retrigger fired:
        # it must be re-sent with control_first = final & ~GATE.
        ctrl = 0x11 | SID.GATE  # pulse-ish + gate on
        last = _img(**{"4": ctrl})
        cur = _img(**{"4": ctrl})
        msgs = frame_messages([cur], [last], retrigger=[(True, False, False)])
        self.assertEqual(len(msgs), 1)
        u = asid.decode(msgs[0])
        assert u is not None
        self.assertEqual(u.regs[0x04], ctrl)
        self.assertEqual(u.control_first, {0: ctrl & ~SID.GATE})

    def test_multi_chip_emits_per_changed_chip(self):
        chip0 = _img(**{"0": 0x01})
        chip1 = _img(**{"0": 0x02})
        msgs = frame_messages([chip0, chip1], [None, None])
        self.assertEqual(len(msgs), 2)
        u0 = asid.decode(msgs[0])
        u1 = asid.decode(msgs[1])
        assert u0 is not None and u1 is not None
        self.assertEqual(u0.chip_index, 0)
        self.assertEqual(u1.chip_index, 1)


class _FakePort:
    """Stand-in for a mido output port: records sent messages, tracks close."""

    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


@unittest.skipUnless(asid_broadcast.MIDI_AVAILABLE, "mido not installed")
class BroadcasterLifecycleTest(unittest.TestCase):
    def _make(self) -> tuple[AsidBroadcaster, _FakePort]:
        b = AsidBroadcaster("fake", system="NTSC")
        port = _FakePort()
        b._port = port  # inject, bypassing _open_port
        return b, port

    def _cmds(self, port: _FakePort) -> list[int]:
        # msg.data = (0x2D, cmd, ...); pull the command byte from each.
        return [tuple(m.data)[1] for m in port.sent]

    def test_start_sends_start_speed_type_text(self):
        b, port = self._make()
        b.start(frame_rate_hz=60.0, chip_types=["8580"], text="TUNE")
        self.assertEqual(
            self._cmds(port),
            [asid.CMD_START, asid.CMD_SPEED, asid.CMD_SID_TYPE, asid.CMD_CHARS],
        )

    def test_send_frame_delta_then_stop(self):
        b, port = self._make()
        b.start(frame_rate_hz=60.0)
        port.sent.clear()
        b.send_frame([_img(**{"0": 0x10})])  # first frame: full
        b.send_frame([_img(**{"0": 0x10})])  # identical: no message
        b.send_frame([_img(**{"0": 0x11})])  # delta: one message
        self.assertEqual(self._cmds(port), [asid.CMD_REG, asid.CMD_REG])
        # The delta frame carried only the changed register.
        u = asid.decode(port.sent[-1].data)
        assert u is not None
        self.assertEqual(u.regs, {0x00: 0x11})
        b.stop()
        self.assertEqual(self._cmds(port)[-1], asid.CMD_STOP)
        self.assertTrue(port.closed)

    def test_start_resets_delta_baseline(self):
        b, port = self._make()
        b.start(frame_rate_hz=60.0)
        b.send_frame([_img(**{"0": 0x10})])
        # A fresh start() must clear the baseline so the next frame is full again.
        b.start(frame_rate_hz=60.0)
        port.sent.clear()
        b.send_frame([_img(**{"0": 0x10})])
        u = asid.decode(port.sent[0].data)
        assert u is not None
        self.assertEqual(u.regs[0x00], 0x10)
        self.assertEqual(len(u.regs), _REG_COUNT)

    def test_send_error_disables_broadcast(self):
        b, port = self._make()

        def boom(_msg):
            raise OSError("port gone")

        port.send = boom  # type: ignore[method-assign]
        with self.assertLogs("c64cast.asid_broadcast", level="WARNING"):
            b.start(frame_rate_hz=60.0)  # first _send raises → disabled
        self.assertTrue(b._disabled)
        # Subsequent calls are silent no-ops (don't raise).
        b.send_frame([_img(**{"0": 0x10})])


@unittest.skipUnless(asid_broadcast.MIDI_AVAILABLE, "mido not installed")
class BroadcasterConstructTest(unittest.TestCase):
    def test_requires_port_name_matchable(self):
        # Constructing is fine; opening a non-existent port raises. We only
        # assert construction here (opening needs a real backend).
        b = AsidBroadcaster("no-such-port")
        self.assertEqual(b.port_name, "no-such-port")


if __name__ == "__main__":
    unittest.main()
