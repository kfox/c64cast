"""Tests for the WLED pixel-sink (bridge Mode 2).

Covers the pure DDP + WLED-realtime packet parsers, the PixelFrameAssembler
(byte-offset writes, RGB→BGR, clipping), the WledPixelReceiver over a loopback
socket (send real bytes, assert the published frame), the WLEDSource lifecycle,
and the config wiring (validation, scene build, display default).
"""

from __future__ import annotations

import socket
import struct
import time
import unittest
from typing import cast

from c64cast.backend import C64Backend, HardwareProfile
from c64cast.config import Config, SceneCfg, build_scene, resolve_scene_display, validate_scene_cfg
from c64cast.scenes import SourceScene
from c64cast.wled_sink import (
    DdpPacket,
    PixelFrameAssembler,
    WledPixelReceiver,
    WLEDSource,
    parse_ddp,
    parse_wled_realtime,
)


def _ddp(offset: int, payload: bytes, *, push: bool = True) -> bytes:
    flags = 0x40 | (0x01 if push else 0x00)
    return struct.pack(">BBBBIH", flags, 0, 0, 1, offset, len(payload)) + payload


# --- DDP parser -------------------------------------------------------------


class DdpParserTest(unittest.TestCase):
    def test_single_packet(self):
        pkt = parse_ddp(_ddp(0, bytes([1, 2, 3, 4, 5, 6])))
        self.assertEqual(pkt, DdpPacket(offset=0, payload=bytes([1, 2, 3, 4, 5, 6]), push=True))

    def test_offset_and_no_push(self):
        pkt = parse_ddp(_ddp(300, bytes([9, 9, 9]), push=False))
        assert pkt is not None
        self.assertEqual(pkt.offset, 300)
        self.assertFalse(pkt.push)

    def test_too_short_is_none(self):
        self.assertIsNone(parse_ddp(b"\x40\x00"))

    def test_wrong_version_is_none(self):
        # flags without the 0x40 version bit → not a V1 data packet.
        self.assertIsNone(parse_ddp(struct.pack(">BBBBIH", 0x00, 0, 0, 1, 0, 0)))

    def test_query_and_reply_rejected(self):
        for flag in (0x08, 0x04):  # query, reply
            dg = struct.pack(">BBBBIH", 0x40 | flag, 0, 0, 1, 0, 0)
            self.assertIsNone(parse_ddp(dg), f"flag {flag:#x} should be rejected")

    def test_length_truncates_payload(self):
        # header claims 3 bytes but 6 are present → only 3 returned.
        dg = struct.pack(">BBBBIH", 0x41, 0, 0, 1, 0, 3) + bytes([1, 2, 3, 4, 5, 6])
        pkt = parse_ddp(dg)
        assert pkt is not None
        self.assertEqual(pkt.payload, bytes([1, 2, 3]))


# --- WLED realtime parser ---------------------------------------------------


class WledRealtimeParserTest(unittest.TestCase):
    def test_drgb(self):
        # proto=2, timeout, then RGB triples from pixel 0.
        w = parse_wled_realtime(bytes([2, 1]) + bytes([10, 20, 30, 40, 50, 60]))
        self.assertEqual(w, [(0, 10, 20, 30), (1, 40, 50, 60)])

    def test_warls(self):
        # proto=1: [index, r, g, b] tuples.
        w = parse_wled_realtime(bytes([1, 1]) + bytes([5, 10, 20, 30, 9, 1, 2, 3]))
        self.assertEqual(w, [(5, 10, 20, 30), (9, 1, 2, 3)])

    def test_drgbw_drops_white(self):
        w = parse_wled_realtime(bytes([3, 1]) + bytes([10, 20, 30, 99, 40, 50, 60, 88]))
        self.assertEqual(w, [(0, 10, 20, 30), (1, 40, 50, 60)])

    def test_dnrgb_start_index(self):
        # proto=4, timeout, start=258 (0x0102), then one RGB.
        w = parse_wled_realtime(bytes([4, 1, 0x01, 0x02]) + bytes([7, 8, 9]))
        self.assertEqual(w, [(258, 7, 8, 9)])

    def test_unknown_proto_is_none(self):
        self.assertIsNone(parse_wled_realtime(bytes([99, 0, 1, 2, 3])))

    def test_short_datagram_is_none(self):
        self.assertIsNone(parse_wled_realtime(b"\x02"))

    def test_trailing_partial_pixel_dropped(self):
        # 4 body bytes for DRGB = one full triple + a stray byte (ignored).
        w = parse_wled_realtime(bytes([2, 1]) + bytes([1, 2, 3, 4]))
        self.assertEqual(w, [(0, 1, 2, 3)])


# --- assembler --------------------------------------------------------------


class AssemblerTest(unittest.TestCase):
    def test_ddp_bytes_to_bgr(self):
        a = PixelFrameAssembler(2, 1)
        a.apply_ddp(0, bytes([255, 0, 0, 0, 255, 0]))  # red, green (RGB)
        f = a.snapshot_bgr()
        self.assertEqual(f.shape, (1, 2, 3))
        self.assertEqual(list(f[0, 0]), [0, 0, 255])  # red → BGR
        self.assertEqual(list(f[0, 1]), [0, 255, 0])  # green → BGR

    def test_ddp_offset_write(self):
        a = PixelFrameAssembler(2, 1)
        a.apply_ddp(3, bytes([1, 2, 3]))  # second pixel only
        f = a.snapshot_bgr()
        self.assertEqual(list(f[0, 0]), [0, 0, 0])
        self.assertEqual(list(f[0, 1]), [3, 2, 1])

    def test_ddp_out_of_range_clipped(self):
        a = PixelFrameAssembler(1, 1)
        a.apply_ddp(0, bytes([1, 2, 3, 4, 5, 6]))  # 2 px of data into a 1px buf
        f = a.snapshot_bgr()
        self.assertEqual(list(f[0, 0]), [3, 2, 1])  # only the first pixel kept
        # An offset past the buffer is a no-op, not an error.
        a.apply_ddp(999, bytes([7, 7, 7]))

    def test_apply_pixels_and_index_bounds(self):
        a = PixelFrameAssembler(2, 1)
        a.apply_pixels([(0, 10, 20, 30), (1, 40, 50, 60), (99, 1, 1, 1)])
        f = a.snapshot_bgr()
        self.assertEqual(list(f[0, 0]), [30, 20, 10])
        self.assertEqual(list(f[0, 1]), [60, 50, 40])


# --- receiver over loopback -------------------------------------------------


class ReceiverTest(unittest.TestCase):
    def _make(self) -> WledPixelReceiver:
        # Ephemeral ports on loopback so the fixed 4048/21324 aren't required.
        rx = WledPixelReceiver(2, 1, host="127.0.0.1", ddp_port=0, wled_port=0)
        self.assertTrue(rx.start())
        self.addCleanup(rx.stop)
        return rx

    def _wait_frame(self, rx: WledPixelReceiver, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            f = rx.latest()
            if f is not None:
                return f
            time.sleep(0.01)
        self.fail("no frame published within timeout")

    def test_ddp_frame_received(self):
        rx = self._make()
        ports = rx.bound_ports()
        assert ports is not None
        ddp_port, _ = ports
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(_ddp(0, bytes([255, 0, 0, 0, 255, 0])), ("127.0.0.1", ddp_port))
        f = self._wait_frame(rx)
        self.assertEqual(list(f[0, 0]), [0, 0, 255])
        self.assertEqual(list(f[0, 1]), [0, 255, 0])

    def test_wled_realtime_frame_received(self):
        rx = self._make()
        ports = rx.bound_ports()
        assert ports is not None
        _, wled_port = ports
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(bytes([2, 1]) + bytes([1, 2, 3, 4, 5, 6]), ("127.0.0.1", wled_port))
        f = self._wait_frame(rx)
        self.assertEqual(list(f[0, 0]), [3, 2, 1])
        self.assertEqual(list(f[0, 1]), [6, 5, 4])

    def test_bind_conflict_reports_error(self):
        # Occupy a port, then a receiver told to use it fails to start.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        self.addCleanup(blocker.close)
        busy = blocker.getsockname()[1]
        rx = WledPixelReceiver(2, 1, host="127.0.0.1", ddp_port=busy, wled_port=0)
        with self.assertLogs("c64cast.wled_sink", level="WARNING"):
            self.assertFalse(rx.start())
        self.assertIsNotNone(rx.bind_error)


# --- WLEDSource lifecycle ---------------------------------------------------


class WLEDSourceTest(unittest.TestCase):
    def test_lifecycle_none_until_frame(self):
        src = WLEDSource(2, 1, host="127.0.0.1")
        # Rebind the receiver onto ephemeral ports before setup.
        src._receiver = WledPixelReceiver(2, 1, host="127.0.0.1", ddp_port=0, wled_port=0)
        src.setup()
        self.addCleanup(src.teardown)
        self.assertFalse(src.finished)
        self.assertIsNone(src.read(0.0))  # nothing received yet
        ports = src._receiver.bound_ports()
        assert ports is not None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(_ddp(0, bytes([255, 0, 0, 0, 255, 0])), ("127.0.0.1", ports[0]))
        deadline = time.time() + 2.0
        while time.time() < deadline and src.read(0.0) is None:
            time.sleep(0.01)
        f = src.read(0.0)
        assert f is not None
        self.assertEqual(list(f[0, 0]), [0, 0, 255])

    def test_bind_failure_marks_finished(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        self.addCleanup(blocker.close)
        busy = blocker.getsockname()[1]
        src = WLEDSource(2, 1)
        src._receiver = WledPixelReceiver(2, 1, host="127.0.0.1", ddp_port=busy, wled_port=0)
        with self.assertLogs("c64cast.wled_sink", level="WARNING"):
            src.setup()
        self.addCleanup(src.teardown)
        self.assertTrue(src.finished)  # scene will self-abort


# --- config wiring ----------------------------------------------------------


class _DummyAPI:
    profile = HardwareProfile(name="Dummy", family="fake")

    def __getattr__(self, name):
        raise AssertionError(f"api.{name} should not be called at build time")


class ConfigWledSinkTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_display_default_is_mhires(self):
        self.assertEqual(resolve_scene_display(None, "wled"), "mhires")

    def test_build_produces_source_scene(self):
        s = SceneCfg(type="wled", display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsInstance(scene.source, WLEDSource)

    def test_build_custom_dimensions(self):
        s = SceneCfg(type="wled", display="mhires", sink_width=64, sink_height=48)
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        recv = cast(WLEDSource, scene.source)._receiver
        self.assertEqual((recv._assembler.width, recv._assembler.height), (64, 48))

    def test_reject_blank_display(self):
        s = SceneCfg(type="wled", display="blank")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_reject_random_display(self):
        s = SceneCfg(type="wled", display="random")
        with self.assertRaises(ValueError):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_reject_out_of_range_dimensions(self):
        for w, h in ((0, 200), (200, 0), (2000, 200)):
            s = SceneCfg(type="wled", display="mhires", sink_width=w, sink_height=h)
            with self.assertRaises(ValueError):
                validate_scene_cfg(s, self.cfg, audio_enabled=False)

    def test_effect_allowed(self):
        # wled is a frame-bearing scene, so a pixel effect validates (no raise).
        s = SceneCfg(type="wled", display="mhires", effect="trails")
        validate_scene_cfg(s, self.cfg, audio_enabled=False)
        scene = build_scene(s, self.cfg, cast(C64Backend, _DummyAPI()), None, None)
        assert isinstance(scene, SourceScene)
        self.assertIsNotNone(scene.effect)


if __name__ == "__main__":
    unittest.main()
