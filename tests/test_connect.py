"""Tests for the scheme-aware connection-target parser (c64cast.connect).

Pure string parsing — no hardware, no config file. Covers the u64/http/tr
schemes, the tr:// serial-vs-TCP disambiguation, ?query knobs, error cases, and
the apply_to_config overlay onto a real Config (so only specified fields move).
"""

from __future__ import annotations

import unittest

from c64cast.config import Config
from c64cast.connect import ConnectionURIError, apply_to_config, parse_connection_uri


class ParseUltimateTest(unittest.TestCase):
    def test_u64_host(self):
        spec = parse_connection_uri("u64://192.168.2.64")
        self.assertEqual(spec.backend, "ultimate")
        self.assertEqual(spec.url, "http://192.168.2.64")
        self.assertIsNone(spec.dma_port)

    def test_u64_host_with_rest_port(self):
        spec = parse_connection_uri("u64://192.168.2.64:8080")
        self.assertEqual(spec.url, "http://192.168.2.64:8080")

    def test_u64_dma_port_query(self):
        spec = parse_connection_uri("u64://host?dma_port=64")
        self.assertEqual(spec.url, "http://host")
        self.assertEqual(spec.dma_port, 64)

    def test_http_passthrough(self):
        spec = parse_connection_uri("http://192.168.2.64")
        self.assertEqual(spec.backend, "ultimate")
        self.assertEqual(spec.url, "http://192.168.2.64")

    def test_https_passthrough(self):
        spec = parse_connection_uri("https://u64.lan")
        self.assertEqual(spec.url, "https://u64.lan")

    def test_u64_needs_host(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("u64://")


class ParseTeensyromTest(unittest.TestCase):
    def test_bare_tr_is_serial_autodetect(self):
        spec = parse_connection_uri("tr://")
        self.assertEqual(spec.backend, "teensyrom")
        self.assertEqual(spec.transport, "serial")
        self.assertIsNone(spec.serial_port)  # None => make_backend auto-detects

    def test_serial_device_path(self):
        spec = parse_connection_uri("tr:///dev/cu.usbmodem1234")
        self.assertEqual(spec.transport, "serial")
        self.assertEqual(spec.serial_port, "/dev/cu.usbmodem1234")

    def test_windows_com_port_is_serial(self):
        spec = parse_connection_uri("tr://COM3")
        self.assertEqual(spec.transport, "serial")
        self.assertEqual(spec.serial_port, "COM3")  # case + value preserved

    def test_tcp_host(self):
        spec = parse_connection_uri("tr://192.168.2.70")
        self.assertEqual(spec.transport, "tcp")
        self.assertEqual(spec.host, "192.168.2.70")
        self.assertIsNone(spec.tcp_port)

    def test_tcp_host_with_port(self):
        spec = parse_connection_uri("tr://teensy.lan:2113")
        self.assertEqual(spec.transport, "tcp")
        self.assertEqual(spec.host, "teensy.lan")
        self.assertEqual(spec.tcp_port, 2113)

    def test_serial_query_knobs(self):
        spec = parse_connection_uri("tr:///dev/x?baud=1500000&storage=usb")
        self.assertEqual(spec.baud, 1500000)
        self.assertEqual(spec.storage, "usb")

    def test_tcp_port_query(self):
        spec = parse_connection_uri("tr://host?tcp_port=2200")
        self.assertEqual(spec.tcp_port, 2200)


class ParseErrorTest(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("   ")

    def test_no_scheme(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("192.168.2.64")
        self.assertIn("scheme", str(cm.exception))

    def test_unknown_scheme(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("ftp://nope")
        self.assertIn("unknown scheme", str(cm.exception))

    def test_bad_int_query(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("u64://host?dma_port=notanumber")


class ApplyToConfigTest(unittest.TestCase):
    def test_tr_serial_overlays_only_relevant_fields(self):
        cfg = Config()
        cfg.ultimate64.url = "http://keep-me.lan"  # must survive a tr:// apply
        apply_to_config(cfg, parse_connection_uri("tr:///dev/cu.usbmodem1"))
        self.assertEqual(cfg.hardware.backend, "teensyrom")
        self.assertEqual(cfg.teensyrom.transport, "serial")
        self.assertEqual(cfg.teensyrom.serial_port, "/dev/cu.usbmodem1")
        # Untouched: u64 url is left as-is (not cleared) — only spec.non-None
        # fields move.
        self.assertEqual(cfg.ultimate64.url, "http://keep-me.lan")

    def test_u64_overlays_url_and_backend(self):
        cfg = Config()
        apply_to_config(cfg, parse_connection_uri("u64://10.0.0.5?dma_port=8064"))
        self.assertEqual(cfg.hardware.backend, "ultimate")
        self.assertEqual(cfg.ultimate64.url, "http://10.0.0.5")
        self.assertEqual(cfg.ultimate64.dma_port, 8064)

    def test_bare_tr_leaves_serial_port_default(self):
        cfg = Config()
        before = cfg.teensyrom.serial_port
        apply_to_config(cfg, parse_connection_uri("tr://"))
        self.assertEqual(cfg.teensyrom.serial_port, before)  # untouched => auto-detect


if __name__ == "__main__":
    unittest.main()
