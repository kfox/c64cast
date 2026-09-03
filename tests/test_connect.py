"""Tests for the scheme-aware connection-target parser (c64cast.app.connect).

Pure string parsing — no hardware, no config file. Covers the u64/http/tr
schemes, the tr:// serial-vs-TCP disambiguation, ?query knobs, error cases, and
the apply_to_config overlay onto a real Config (so only specified fields move).
"""

from __future__ import annotations

import unittest

from c64cast.app import connect
from c64cast.app.config import Config
from c64cast.app.connect import ConnectionURIError, apply_to_config, parse_connection_uri


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

    def test_http_passthrough_strips_query(self):
        # The query is consumed into `dma_port`, not left in the base URL that
        # Ultimate64API concatenates every REST path onto.
        spec = parse_connection_uri("http://192.168.2.64?dma_port=64")
        self.assertEqual(spec.url, "http://192.168.2.64")
        self.assertEqual(spec.dma_port, 64)

    def test_u64_bad_port(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("u64://192.168.2.64:notaport")
        self.assertIn("bad port", str(cm.exception))

    def test_http_bad_port(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("http://192.168.2.64:notaport")
        self.assertIn("bad port", str(cm.exception))


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

    def test_bad_port_in_netloc(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("tr://host:notaport")
        self.assertIn("bad port", str(cm.exception))

    def test_tcp_port_query_validated_even_with_netloc_port(self):
        # Regression: `port or _int_query(...)` used to short-circuit past
        # this validation whenever the netloc already carried a port.
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("tr://host:2113?tcp_port=notanumber")


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

    def test_unknown_query_key_rejected(self):
        # A typo'd knob (dmaport for dma_port) used to be parsed as absent and
        # do nothing, with no diagnostic.
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("u64://host?dmaport=64")
        self.assertIn("dmaport", str(cm.exception))

    def test_blank_query_value_rejected(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("u64://host?dma_port=")


class UserinfoRejectedTest(unittest.TestCase):
    """A target must never carry a username/password — the Ultimate's REST
    API has no HTTP auth of its own, `requests` would send it as a Basic-auth
    header on every call regardless, and accepting it would let
    --save-settings persist + echo the credential in plaintext."""

    def test_u64_userinfo_rejected(self):
        with self.assertRaises(ConnectionURIError) as cm:
            parse_connection_uri("u64://admin:s3cret@192.168.2.64")
        self.assertIn("username/password", str(cm.exception))

    def test_http_userinfo_rejected(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("http://admin:s3cret@u64.lan")

    def test_tr_userinfo_rejected(self):
        with self.assertRaises(ConnectionURIError):
            parse_connection_uri("tr://admin:s3cret@teensy.lan")

    def test_the_refusal_does_not_write_the_credential_to_the_log(self):
        # cli.py logs a ConnectionURIError at error level and --log-file
        # mirrors it to disk, so the guard whose whole purpose is keeping the
        # credential off those paths was itself putting it there.
        for target in (
            "u64://admin:s3cret@192.168.2.64",
            "http://admin:s3cret@u64.lan",
            "tr://admin:s3cret@teensy.lan",
        ):
            with self.assertRaises(ConnectionURIError) as cm:
                parse_connection_uri(target)
            self.assertNotIn("s3cret", str(cm.exception), target)
            # The host is still named, so the message stays diagnostic.
            self.assertIn("REDACTED", str(cm.exception), target)


class RedactTargetTest(unittest.TestCase):
    """`redact_target` is what every parse failure reports the target through."""

    def test_an_ordinary_target_is_unchanged(self):
        for target in ("u64://192.168.2.64", "http://u64.lan:8080/x", "tr:///dev/cu.usbmodem1"):
            self.assertEqual(connect.redact_target(target), target)

    def test_a_password_in_the_netloc_is_masked_with_the_host_kept(self):
        out = connect.redact_target("u64://admin:s3cret@192.168.2.64")
        self.assertNotIn("s3cret", out)
        self.assertNotIn("admin", out)
        self.assertIn("192.168.2.64", out)

    def test_a_secret_looking_query_value_is_masked(self):
        out = connect.redact_target("u64://u64.lan?token=abc123")
        self.assertNotIn("abc123", out)


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
