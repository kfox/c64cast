"""Tests for the Ultimate 64's own VIC stream: decode, reassembly, lifetime.

No hardware and no network. The receiver is driven by feeding packets to
`_accept`, which is where every interesting decision is; the socket around it
is `recv` in a loop and adds nothing a test could pin down that the OS doesn't
already guarantee.

What these are *for* is the wire format, which is the part of this feature that
cannot be reasoned out from the c64cast side: it is the firmware's, it is
undocumented outside the firmware source and one reference client, and getting
the nibble order or the end-of-frame bit wrong produces a picture that looks
plausible and is wrong. `unpack_pixels` is asserted against a hand-built packet
rather than against itself.

Not covered here: the socket-DMA commands reaching a real machine, and the
firmware's own auto-stop timer expiring — both are hardware, and the
hw-visual-verify pass is where they are exercised."""

from __future__ import annotations

import struct
import unittest

import numpy as np

from c64cast.hw import vic_stream
from c64cast.hw.socket_dma import CMD_VICSTREAM_OFF, CMD_VICSTREAM_ON, STREAM_TICK_S

WIDTH = 384
LINE_BYTES = WIDTH // 2


def packet(*, seq: int, frame: int, line: int, payload: bytes, width: int = WIDTH) -> bytes:
    """One stream packet: the four fields the receiver reads, four bytes it
    ignores, then the pixels."""
    return struct.pack("<HHHH", seq, frame, line, width) + b"\x00\x00\x00\x00" + payload


class UnpackTest(unittest.TestCase):
    def test_two_pixels_a_byte_low_nibble_first(self):
        # The one fact a wrong implementation gets backwards while still
        # producing a picture: 0x21 is pixel 1 then pixel 2, not 2 then 1.
        out = vic_stream.unpack_pixels(bytes([0x21, 0xF0]), width=4)
        self.assertEqual(out.tolist(), [[1, 2, 0, 15]])

    def test_height_comes_from_how_much_arrived(self):
        out = vic_stream.unpack_pixels(b"\x00" * (LINE_BYTES * 3), width=WIDTH)
        self.assertEqual(out.shape, (3, WIDTH))

    def test_a_trailing_partial_line_is_dropped_not_padded(self):
        out = vic_stream.unpack_pixels(b"\x11" * (LINE_BYTES * 2 + 5), width=WIDTH)
        self.assertEqual(out.shape, (2, WIDTH))

    def test_every_value_is_a_palette_index(self):
        out = vic_stream.unpack_pixels(bytes(range(256)) * 2, width=WIDTH)
        self.assertTrue((out <= 15).all())

    def test_a_payload_under_one_line_is_refused(self):
        with self.assertRaises(ValueError):
            vic_stream.unpack_pixels(b"\x00" * 10, width=WIDTH)

    def test_an_impossible_width_is_refused(self):
        for width in (0, -2, 383):
            with self.assertRaises(ValueError):
                vic_stream.unpack_pixels(b"\x00" * 200, width=width)


class _FakeDMA:
    """Records the two stream commands without a socket."""

    def __init__(self) -> None:
        self.started: list[tuple[str, float]] = []
        self.stopped = 0

    def vicstream_on(self, destination: str, *, stop_after_s: float = 0.0) -> None:
        self.started.append((destination, stop_after_s))

    def vicstream_off(self) -> None:
        self.stopped += 1


class ReassemblyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dma = _FakeDMA()
        self.rx = vic_stream.VicStreamReceiver(self.dma, machine_host="192.0.2.64")

    def _feed(self, lines: int, *, frame: int, fill: int = 0x11, end: bool = True) -> None:
        for i in range(lines):
            last = end and i == lines - 1
            self.rx._accept(
                packet(
                    seq=i,
                    frame=frame,
                    line=(i | vic_stream._LAST_PACKET) if last else i,
                    payload=bytes([fill]) * LINE_BYTES,
                )
            )

    def test_a_frame_lands_only_when_the_last_packet_says_so(self):
        self._feed(4, frame=7, end=False)
        self.assertIsNone(self.rx.latest())
        self._feed(1, frame=7)
        frame = self.rx.latest()
        assert frame is not None
        self.assertEqual((frame.height, frame.width), (5, WIDTH))
        self.assertEqual(frame.number, 7)

    def test_the_frame_carries_the_indices_that_arrived(self):
        self._feed(2, frame=1, fill=0x9A)
        frame = self.rx.latest()
        assert frame is not None
        # 0x9A is pixel 10 then pixel 9 — low nibble first, all the way through.
        self.assertEqual(frame.indices[0, 0], 10)
        self.assertEqual(frame.indices[0, 1], 9)

    def test_only_the_latest_frame_is_kept(self):
        self._feed(2, frame=1)
        self._feed(2, frame=2)
        frame = self.rx.latest()
        assert frame is not None
        self.assertEqual(frame.number, 2)
        self.assertEqual(self.rx.stats["frames"], 2)

    def test_a_width_change_discards_the_frame_in_hand(self):
        # A mode change on the machine: the lines already collected were
        # measured in the old width and joining them to the new ones would
        # produce a picture that is wrong in a way that looks deliberate.
        self.rx._accept(packet(seq=0, frame=1, line=0, payload=b"\x11" * LINE_BYTES))
        self.rx._accept(
            packet(
                seq=1, frame=1, line=1 | vic_stream._LAST_PACKET, payload=b"\x22" * 160, width=320
            )
        )
        frame = self.rx.latest()
        assert frame is not None
        self.assertEqual(frame.width, 320)
        self.assertEqual(frame.height, 1)

    def test_a_short_packet_is_ignored_rather_than_unpacked(self):
        self.rx._accept(b"\x00" * 8)
        self.assertIsNone(self.rx.latest())

    def test_a_frame_whose_end_never_arrives_expires(self):
        self._feed(3, frame=1, end=False)
        self.rx._last_packet_at -= vic_stream._STALE_FRAME_S + 1
        self.rx._expire_partial()
        self.assertEqual(self.rx.stats["dropped"], 1)
        # And the next frame is a frame, not those three lines plus these.
        self._feed(2, frame=2)
        frame = self.rx.latest()
        assert frame is not None
        self.assertEqual(frame.height, 2)

    def test_an_unfinished_frame_that_is_still_arriving_is_left_alone(self):
        self._feed(3, frame=1, end=False)
        self.rx._expire_partial()
        self.assertEqual(self.rx.stats["dropped"], 0)


class WatchdogTest(unittest.TestCase):
    """The machine counts its own stop timer down, which is the only kind that
    survives this process being killed outright."""

    def setUp(self) -> None:
        self.dma = _FakeDMA()
        self.rx = vic_stream.VicStreamReceiver(self.dma, machine_host="192.0.2.64")
        self.rx._destination = "192.0.2.1:40000"

    def test_the_rearm_renews_it_rather_than_asking_for_forever(self):
        self.rx._rearm_at = 0.0
        self.rx._maybe_rearm()
        self.assertEqual(self.dma.started, [("192.0.2.1:40000", vic_stream.WATCHDOG_S)])
        # And not again until the interval is up.
        self.rx._maybe_rearm()
        self.assertEqual(len(self.dma.started), 1)

    def test_the_window_outlives_the_gap_between_renewals(self):
        # Otherwise the stream stops between re-arms and the picture stutters
        # once every interval — a slow failure that reads as a network problem.
        self.assertGreater(vic_stream.WATCHDOG_S, vic_stream.REARM_EVERY_S * 2)

    def test_a_link_failure_while_renewing_is_not_fatal(self):
        def boom(destination: str, *, stop_after_s: float = 0.0) -> None:
            raise OSError("link went away")

        self.dma.vicstream_on = boom  # type: ignore[method-assign]
        self.rx._rearm_at = 0.0
        self.rx._maybe_rearm()  # must not raise; the next round renews


class StreamCommandTest(unittest.TestCase):
    """The two socket-DMA opcodes, and the duration's unit — which is the
    firmware's FreeRTOS tick and not seconds or milliseconds."""

    def setUp(self) -> None:
        from c64cast.hw import socket_dma

        self.sent: list[tuple[int, bytes]] = []
        self.client = socket_dma.SocketDMAClient.__new__(socket_dma.SocketDMAClient)
        self.client._send_with_reconnect = lambda op, payload: self.sent.append((op, payload))  # type: ignore[method-assign]

    def test_on_carries_the_duration_then_the_bare_destination(self):
        self.client.vicstream_on("192.0.2.1:40000", stop_after_s=20.0)
        opcode, payload = self.sent[0]
        self.assertEqual(opcode, CMD_VICSTREAM_ON)
        ticks = struct.unpack("<H", payload[:2])[0]
        self.assertEqual(ticks, round(20.0 / STREAM_TICK_S))
        # The firmware NUL-terminates the name itself at the command length, so
        # a terminator here would land inside the hostname it parses.
        self.assertEqual(payload[2:], b"192.0.2.1:40000")

    def test_zero_means_unbounded_which_is_what_the_firmware_reads_it_as(self):
        self.client.vicstream_on("host:1", stop_after_s=0.0)
        self.assertEqual(struct.unpack("<H", self.sent[0][1][:2])[0], 0)

    def test_a_duration_past_the_field_is_clamped_not_wrapped(self):
        # uint16 of 5 ms ticks tops out around five and a half minutes; wrapping
        # would turn "keep going for an hour" into "stop almost immediately".
        self.client.vicstream_on("host:1", stop_after_s=86400.0)
        self.assertEqual(struct.unpack("<H", self.sent[0][1][:2])[0], 0xFFFF)

    def test_off_takes_no_payload(self):
        self.client.vicstream_off()
        self.assertEqual(self.sent[0], (CMD_VICSTREAM_OFF, b""))


class ProfileGateTest(unittest.TestCase):
    """Which machines can do this, and how that is decided."""

    def test_the_ultimate_family_claims_it_and_the_teensyrom_does_not(self):
        from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE

        self.assertTrue(ULTIMATE_PROFILE.supports_video_stream)
        self.assertFalse(TEENSYROM_PROFILE.supports_video_stream)

    def test_it_follows_the_same_category_as_system_mode(self):
        # Both are compiled under the firmware's `#ifdef U64`, so a device
        # registering one registers the other — a U2+ has neither. Asking twice
        # would only create a way for them to disagree.
        import inspect

        from c64cast.hw.api import Ultimate64API

        source = inspect.getsource(Ultimate64API.refine_capabilities)
        self.assertIn("supports_video_stream=has_system_mode", source)

    def test_a_frame_is_indices_rather_than_colour(self):
        # So a caller comparing against what c64cast meant to draw compares
        # indices with indices, and a caller displaying it picks the palette.
        frame = vic_stream.VicFrame(np.zeros((2, 4), dtype=np.uint8), 1, 0.0)
        self.assertEqual(frame.indices.dtype, np.uint8)
        self.assertEqual((frame.height, frame.width), (2, 4))


if __name__ == "__main__":
    unittest.main()
