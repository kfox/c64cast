"""Unit tests for overlays — no real U64, no real audio, no network."""

# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import time
import unittest
from typing import cast
from unittest.mock import MagicMock, patch

import numpy as np

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
from _fakes import FakeAPI

from c64cast.backend import C64Backend
from c64cast.overlays import (
    Overlay,
    ascii_to_screen,
    build_overlay,
    known_overlays,
    validate_for_scene,
)


def _fake_api() -> C64Backend:
    """A FakeAPI typed as the C64Backend the overlay methods expect. The
    fake is a structural stand-in (not a nominal subclass), so cast at the
    boundary — these tests only drive setup/compose/teardown, not the
    FakeAPI-specific snapshot attributes."""
    return cast(C64Backend, FakeAPI())


def _make_buffers():
    """Fresh buffers dict matching what CharDisplayMode.compose() returns —
    including the CharTextSurface text overlays now paint through (its writes
    pass through to the same screen/color arrays, so the assertions below
    still inspect buffers["screen"]/["color"] directly)."""
    from c64cast.overlays import SC_SPACE
    from c64cast.text_surface import CharTextSurface

    screen = np.full(40 * 25, SC_SPACE, dtype=np.uint8)
    color = np.zeros(40 * 25, dtype=np.uint8)
    return {"screen": screen, "color": color, "text": CharTextSurface(screen, color)}


class FakeAudio:
    """Hands back a pre-canned numpy array on get_recent_samples."""

    def __init__(self, samples: np.ndarray, sample_rate: int = 8000):
        self.samples = samples.astype(np.float32)
        self.sample_rate = sample_rate

    def get_recent_samples(self, n: int) -> np.ndarray:
        if n <= self.samples.size:
            return self.samples[-n:].copy()
        out = np.zeros(n, dtype=np.float32)
        out[-self.samples.size :] = self.samples
        return out


class FakePetsciiMode:
    is_bitmapped = False
    is_petscii_compatible = True
    name = "petscii"


class FakeMcmMode:
    """Char mode that is NOT petscii — PETSCII overlays must refuse it
    because MCM reinterprets color-RAM bit 3 + character pixel pairs."""

    is_bitmapped = False
    is_petscii_compatible = False
    name = "mcm"


class FakeBitmapMode:
    """A real bitmap mode (hires/mhires): not PETSCII-compatible, but text
    overlays fold their glyphs into the bitmap, so is_bitmap_text_compatible."""

    is_bitmapped = True
    is_petscii_compatible = False
    is_bitmap_text_compatible = True
    name = "fake_bitmap"


# ---------------------------------------------------------------------------
# Registry + validation
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_known_overlays(self):
        names = known_overlays()
        for expected in ("scrolling_text", "spectrum_petscii", "clock", "weather"):
            self.assertIn(expected, names)

    def test_build_clock_overlay(self):
        ov = build_overlay({"type": "clock", "corner": "top-right"}, audio=None)
        self.assertEqual(ov.name, "clock")

    def test_build_unknown_type_raises(self):
        with self.assertRaises(ValueError) as cm:
            build_overlay({"type": "nope"}, audio=None)
        self.assertIn("nope", str(cm.exception))

    def test_audio_overlay_without_audio_raises(self):
        with self.assertRaises(ValueError) as cm:
            build_overlay({"type": "spectrum_petscii"}, audio=None)
        self.assertIn("audio", str(cm.exception).lower())

    def test_validate_for_scene_accepts_bitmap_text_overlay(self):
        # Text overlays (clock/marquee/…) now fold glyphs into the bitmap, so
        # they attach to a bitmap-text-compatible mode (hires/mhires).
        ov = build_overlay({"type": "clock"}, audio=None)
        validate_for_scene(ov, FakeBitmapMode())  # no raise

    def test_validate_for_scene_rejects_bitmap_for_non_text_overlay(self):
        # A REQUIRES_PETSCII overlay that does NOT fold glyphs (e.g. the
        # spectrum bar renderer) stays petscii/blank-only.
        class _ArtOverlay(Overlay):
            name = "art"
            REQUIRES_PETSCII = True
            SUPPORTS_BITMAP_TEXT = False

        with self.assertRaises(ValueError):
            validate_for_scene(_ArtOverlay(), FakeBitmapMode())

    def test_validate_for_scene_rejects_mcm_for_petscii_overlay(self):
        # MCM is neither PETSCII- nor bitmap-text-compatible (color-RAM bit 3
        # toggles multicolor + pixel pairs render at 4x8), so even a folding
        # text overlay can't render there.
        ov = build_overlay({"type": "clock"}, audio=None)
        with self.assertRaises(ValueError) as cm:
            validate_for_scene(ov, FakeMcmMode())
        self.assertIn("petscii", str(cm.exception))

    def test_validate_for_scene_accepts_petscii(self):
        ov = build_overlay({"type": "clock"}, audio=None)
        validate_for_scene(ov, FakePetsciiMode())  # no raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScreenCodeTest(unittest.TestCase):
    def test_uppercase_letters_map_to_low_screen_codes(self):
        # 'A' = ASCII 0x41 → screen code 0x01; 'Z' = 0x5A → 0x1A
        self.assertEqual(ascii_to_screen("AZ"), bytes([0x01, 0x1A]))

    def test_digits_pass_through(self):
        self.assertEqual(ascii_to_screen("0123"), bytes([0x30, 0x31, 0x32, 0x33]))

    def test_space(self):
        self.assertEqual(ascii_to_screen(" "), bytes([0x20]))


# ---------------------------------------------------------------------------
# Scrolling text
# ---------------------------------------------------------------------------


class ScrollingTextTest(unittest.TestCase):
    def test_renders_screen_row_after_setup(self):
        from c64cast.overlays.scrolling_text import ScrollingTextOverlay

        ov = ScrollingTextOverlay(
            messages=[
                {
                    "text": "HI",
                    "color": "yellow",
                    "pre_delay_s": 0.0,
                    "pause_time_s": 0.0,
                    "style": "static",
                }
            ],
            row=10,
            speed_cells_per_s=4.0,
        )
        ov.setup(api=_fake_api(), scene=MagicMock())
        buffers = _make_buffers()
        ov.compose(buffers, scene=MagicMock(), t=ov.start_time + 0.1)
        base = 10 * 40
        row = bytes(buffers["screen"][base : base + 40])
        # "HI" should appear somewhere in the row (centered for static).
        self.assertIn(bytes([0x08, 0x09]), row)  # 'H'(=0x08), 'I'(=0x09)
        # Color row got written too (40 cells).
        self.assertEqual(len(buffers["color"][base : base + 40]), 40)

    def test_empty_messages_raises(self):
        from c64cast.overlays.scrolling_text import ScrollingTextOverlay

        with self.assertRaises(ValueError):
            ScrollingTextOverlay(messages=[])

    def test_bad_row_raises(self):
        from c64cast.overlays.scrolling_text import ScrollingTextOverlay

        with self.assertRaises(ValueError):
            ScrollingTextOverlay(messages=[{"text": "x"}], row=99)


# ---------------------------------------------------------------------------
# Spectrum (PETSCII)
# ---------------------------------------------------------------------------


class SpectrumPetsciiTest(unittest.TestCase):
    def test_zero_samples_produce_no_bars(self):
        from c64cast.overlays import SC_SPACE
        from c64cast.overlays.spectrum_petscii import PetsciiSpectrumOverlay

        audio = FakeAudio(np.zeros(2048, dtype=np.float32))
        ov = PetsciiSpectrumOverlay(audio=audio, placement="bottom", height_rows=8)
        buffers = _make_buffers()
        ov.compose(buffers, scene=MagicMock(), t=0.0)
        # Silence → strip rows should be all-space (no bars).
        top = ov._strip_rows.start * 40
        bot = ov._strip_rows.stop * 40
        self.assertTrue(
            (buffers["screen"][top:bot] == SC_SPACE).all(), "silence shouldn't light any cells"
        )

    def test_loud_tone_lights_bars(self):
        from c64cast.overlays import SC_FULL
        from c64cast.overlays.spectrum_petscii import PetsciiSpectrumOverlay

        # 1 kHz tone (well within band range at 8 kHz sample rate).
        sr = 8000
        t = np.arange(2048) / sr
        tone = 0.8 * np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        audio = FakeAudio(tone, sample_rate=sr)
        ov = PetsciiSpectrumOverlay(audio=audio, placement="bottom", height_rows=12, gain=2.0)
        buffers = _make_buffers()
        ov.compose(buffers, scene=MagicMock(), t=0.0)
        self.assertTrue(
            (buffers["screen"] == SC_FULL).any(), "a loud tone should light at least one bar"
        )


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class ClockTest(unittest.TestCase):
    def test_renders_time_string_to_top_right(self):
        from c64cast.overlays.clock import ClockOverlay

        ov = ClockOverlay(corner="top-right", format="%H:%M", fg_color="white", bg_color="black")
        buffers = _make_buffers()
        with patch("c64cast.overlays.clock.datetime") as dt_mock:
            dt_mock.now.return_value.strftime.return_value = "12:34"
            ov.compose(buffers, scene=MagicMock(), t=0.0)
        # Top-right of 40-col screen for a 5-char string → cols 35..39, row 0.
        # "12:34" → 0x31 0x32 0x3A 0x33 0x34 (digits + ':' pass through)
        self.assertEqual(bytes(buffers["screen"][35:40]), bytes([0x31, 0x32, 0x3A, 0x33, 0x34]))


# ---------------------------------------------------------------------------
# Weather (stubbed)
# ---------------------------------------------------------------------------


class WeatherTest(unittest.TestCase):
    def test_uses_cache_when_fetch_fails(self):
        from c64cast.overlays.weather import WeatherOverlay

        with patch("c64cast.overlays.weather._fetch_open_meteo", side_effect=Exception("boom")):
            ov = WeatherOverlay(provider="open-meteo", lat=0.0, lon=0.0, refresh_minutes=10)
            api = _fake_api()
            # The unexpected-exception path calls log.exception — capture it
            # so the traceback doesn't spam stderr, and verify it fired.
            # Teardown is inside assertLogs so the bg-thread join guarantees
            # the fetch (and its log) completed before the context exits.
            with self.assertLogs("c64cast.overlays.weather", level="ERROR") as cap:
                ov.setup(api, scene=MagicMock())
                buffers = _make_buffers()
                ov.compose(buffers, scene=MagicMock(), t=0.0)
                ov.teardown(api, scene=MagicMock())
        self.assertTrue(
            any("unexpected fetch error" in line for line in cap.output),
            f"expected unexpected-fetch-error log, got: {cap.output!r}",
        )

    def test_renders_cached_value(self):
        from c64cast.overlays.weather import WeatherOverlay

        with patch("c64cast.overlays.weather._fetch_open_meteo", return_value="72F CLEAR"):
            ov = WeatherOverlay(provider="open-meteo", lat=0.0, lon=0.0, refresh_minutes=10)
            api = _fake_api()
            ov.setup(api, scene=MagicMock())
            # Wait for the first poll to populate the cache.
            deadline = time.time() + 1.0
            while ov._cached == "--" and time.time() < deadline:
                time.sleep(0.01)
            buffers = _make_buffers()
            ov.compose(buffers, scene=MagicMock(), t=0.0)
            ov.teardown(api, scene=MagicMock())
        self.assertEqual(ov._cached, "72F CLEAR")
        # Some color cells got painted (non-zero in the buffer).
        self.assertTrue((buffers["color"] != 0).any())

    def test_bad_provider_raises(self):
        from c64cast.overlays.weather import WeatherOverlay

        with self.assertRaises(ValueError):
            WeatherOverlay(provider="nope")

    def test_open_meteo_requires_lat_lon(self):
        from c64cast.overlays.weather import WeatherOverlay

        with self.assertRaises(ValueError):
            WeatherOverlay(provider="open-meteo")


# ---------------------------------------------------------------------------
# Callsign / countdown / network — corner-text family
# ---------------------------------------------------------------------------


class CallsignTest(unittest.TestCase):
    def test_renders_text(self):
        from c64cast.overlays.callsign import CallsignOverlay

        ov = CallsignOverlay(text="W5ABC", corner="bottom-left")
        buffers = _make_buffers()
        ov.compose(buffers, scene=MagicMock(), t=0.0)
        # bottom-left of 40-col, 1-row → screen row 24, col 0..4.
        base = 24 * 40
        # W=0x17, 5=0x35, A=0x01, B=0x02, C=0x03
        self.assertEqual(
            bytes(buffers["screen"][base : base + 5]), bytes([0x17, 0x35, 0x01, 0x02, 0x03])
        )

    def test_empty_text_raises(self):
        from c64cast.overlays.callsign import CallsignOverlay

        with self.assertRaises(ValueError):
            CallsignOverlay(text="")


class CountdownTest(unittest.TestCase):
    def test_remaining_string_includes_seconds(self):
        from datetime import datetime, timedelta

        from c64cast.overlays.countdown import CountdownOverlay

        future = (datetime.now() + timedelta(seconds=125)).isoformat()
        ov = CountdownOverlay(target=future, corner="bottom-left")
        strings = ov.compute_strings(t=0.0)
        # ~125 sec → "00:02:05" (give or take a second)
        assert strings is not None
        self.assertEqual(len(strings), 1)
        self.assertRegex(strings[0], r"^\d{2}:\d{2}:\d{2}$")

    def test_done_after_target(self):
        from datetime import datetime, timedelta

        from c64cast.overlays.countdown import CountdownOverlay

        past = (datetime.now() - timedelta(seconds=60)).isoformat()
        ov = CountdownOverlay(target=past, done_text="DONE")
        self.assertEqual(ov.compute_strings(t=0.0), ["DONE"])

    def test_bad_format_falls_back(self):
        from datetime import datetime, timedelta

        from c64cast.overlays.countdown import CountdownOverlay

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        ov = CountdownOverlay(target=future, format="{nope}")
        # Should not crash; fallback format applies. The bad-format warning
        # fires inside compute_strings on first call — capture + verify.
        with self.assertLogs("c64cast.overlays.countdown", level="WARNING") as cap:
            result = ov.compute_strings(t=0.0)
        self.assertTrue(
            any("bad format" in line for line in cap.output),
            f"expected bad-format warning, got: {cap.output!r}",
        )
        self.assertIsNotNone(result)


class NetworkTest(unittest.TestCase):
    def test_bad_items_raises(self):
        from c64cast.overlays.network import NetworkOverlay

        with self.assertRaises(ValueError):
            NetworkOverlay(items=["ip", "garbage"])

    def test_compute_strings_returns_cached(self):
        from c64cast.overlays.network import NetworkOverlay

        ov = NetworkOverlay(items=["ip"])
        # Without setup() the poll thread doesn't run; cached defaults apply.
        result = ov.compute_strings(t=0.0)
        self.assertEqual(result, ["..."])


# ---------------------------------------------------------------------------
# Marquee + RSS
# ---------------------------------------------------------------------------


class MarqueeTest(unittest.TestCase):
    def test_scrolls_text_across_row(self):
        from c64cast.overlays.marquee import MarqueeOverlay

        ov = MarqueeOverlay(text="ABC", row=5, speed_cells_per_s=10.0)
        ov.setup(api=_fake_api(), scene=MagicMock())
        # First compose at t=start_time → offset 0 should show "ABC"
        # followed by the separator at row 5.
        buffers = _make_buffers()
        ov.compose(buffers, scene=MagicMock(), t=ov.start_time)
        base = 5 * 40
        row = bytes(buffers["screen"][base : base + 40])
        # 'A'=0x01, 'B'=0x02, 'C'=0x03 should appear at the start.
        self.assertEqual(row[:3], bytes([0x01, 0x02, 0x03]))

    def test_empty_text_raises(self):
        from c64cast.overlays.marquee import MarqueeOverlay

        with self.assertRaises(ValueError):
            MarqueeOverlay(text="")


class RssTitleExtractionTest(unittest.TestCase):
    def test_extracts_rss20_titles(self):
        from c64cast.overlays.rss import _extract_titles

        xml = """<?xml version="1.0"?><rss><channel>
        <item><title>First</title></item>
        <item><title>Second</title></item>
        <item><title>Third</title></item>
        </channel></rss>"""
        self.assertEqual(_extract_titles(xml, 2), ["First", "Second"])
        self.assertEqual(_extract_titles(xml, 10), ["First", "Second", "Third"])

    def test_extracts_atom_titles(self):
        from c64cast.overlays.rss import _extract_titles

        xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Atom-1</title></entry>
        <entry><title>Atom-2</title></entry>
        </feed>"""
        self.assertEqual(_extract_titles(xml, 5), ["Atom-1", "Atom-2"])


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------


class LogoTest(unittest.TestCase):
    def test_renders_file_at_corner(self):
        import os
        import tempfile

        from c64cast.overlays.logo import LogoOverlay

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("AB\nCD\n")
            path = f.name
        try:
            ov = LogoOverlay(file=path, corner="top-left")
            buffers = _make_buffers()
            ov.compose(buffers, scene=MagicMock(), t=0.0)
            # Top-left 2-col × 2-row block → rows 0 and 1, col 0.
            self.assertEqual(bytes(buffers["screen"][0:2]), bytes([0x01, 0x02]))  # AB
            self.assertEqual(bytes(buffers["screen"][40:42]), bytes([0x03, 0x04]))  # CD
        finally:
            os.unlink(path)

    def test_missing_file_uses_placeholder(self):
        # Documented behaviour: missing file → placeholder render so the
        # example config can ship with a hint path that still works.
        from c64cast.overlays.logo import LogoOverlay

        with self.assertLogs("c64cast.overlays.logo", level="WARNING") as cap:
            ov = LogoOverlay(file="/nonexistent/path.txt", corner="top-left")
        self.assertTrue(
            any("not found" in line for line in cap.output),
            f"expected missing-file warning, got: {cap.output!r}",
        )
        self.assertTrue(ov._placeholder)
        # Placeholder always renders at least one row and references the
        # missing path so the user notices.
        self.assertGreater(len(ov.lines), 0)
        joined = " ".join(ov.lines).upper()
        self.assertIn("PLACEHOLDER", joined)

    def test_must_specify_position(self):
        import os
        import tempfile

        from c64cast.overlays.logo import LogoOverlay

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("X\n")
            path = f.name
        try:
            with self.assertRaises(ValueError):
                LogoOverlay(file=path)  # neither corner nor row+col
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Network — _collect dispatch + error fallback + setup URL parsing
# ---------------------------------------------------------------------------


class NetworkCollectTest(unittest.TestCase):
    def _ov(self, items):
        from c64cast.overlays.network import NetworkOverlay

        return NetworkOverlay(items=items)

    def test_collect_gathers_each_item(self):
        import c64cast.overlays.network as net

        ov = self._ov(["ip", "hostname", "ping"])
        ov._target_host = "10.0.0.5"
        ov._target_port = 80
        with (
            patch.object(net, "_outbound_ip", return_value="192.168.1.2"),
            patch.object(net, "_tcp_ping_ms", return_value=12.0),
            patch.object(net.socket, "gethostname", return_value="myhost"),
        ):
            lines = ov._collect()
        self.assertEqual(lines[0], "192.168.1.2")
        self.assertEqual(lines[1], "MYHOST")
        self.assertEqual(lines[2], "PING  12MS")

    def test_collect_preserves_previous_value_on_failure(self):
        import c64cast.overlays.network as net

        ov = self._ov(["ip"])
        ov._cached_lines = ["GOOD-IP"]
        with (
            patch.object(net, "_outbound_ip", side_effect=OSError("network down")),
            self.assertLogs("c64cast.overlays.network", level="DEBUG"),
        ):
            lines = ov._collect()
        # The error branch keeps the last good value rather than clobbering.
        self.assertEqual(lines, ["GOOD-IP"])

    def test_poll_once_publishes_to_cache(self):
        import c64cast.overlays.network as net

        ov = self._ov(["ip"])
        with patch.object(net, "_outbound_ip", return_value="1.2.3.4"):
            ov._poll_once()
        self.assertEqual(ov.compute_strings(t=0.0), ["1.2.3.4"])

    def test_setup_parses_ping_target_from_base_url(self):
        ov = self._ov(["ping"])
        api = MagicMock()
        api.base_url = "http://ultimate.lan:8080"
        ov.setup(api, scene=MagicMock())
        try:
            self.assertEqual(ov._target_host, "ultimate.lan")
            self.assertEqual(ov._target_port, 8080)
        finally:
            ov.teardown(api, scene=MagicMock())

    def test_setup_without_base_url_leaves_target_unset(self):
        ov = self._ov(["ping"])
        api = MagicMock(spec=[])  # no base_url attribute
        ov.setup(api, scene=MagicMock())
        try:
            self.assertIsNone(ov._target_host)
        finally:
            ov.teardown(api, scene=MagicMock())


# ---------------------------------------------------------------------------
# OBS status — poll formatting + offline fallback (no real OBS websocket)
# ---------------------------------------------------------------------------


class _FakeOBSClient:
    """Stand-in for obsws-python's ReqClient with canned responses."""

    def __init__(self, scene="LIVE", skipped=2, render_skipped=3):
        self._scene = scene
        self._skipped = skipped
        self._render_skipped = render_skipped
        self.disconnected = False

    def get_current_program_scene(self):
        return type("R", (), {"current_program_scene_name": self._scene})()

    def get_stats(self):
        return type(
            "S",
            (),
            {
                "output_skipped_frames": self._skipped,
                "render_skipped_frames": self._render_skipped,
            },
        )()

    def disconnect(self):
        self.disconnected = True


def _obs_overlay(**kw):
    """Build an OBSStatusOverlay bypassing the obsws-python import guard."""
    import c64cast.overlays.obs_status as obs

    with patch.object(obs, "OBSWS_AVAILABLE", True):
        return obs.OBSStatusOverlay(**kw)


class OBSStatusTest(unittest.TestCase):
    def test_construction_requires_extra(self):
        import c64cast.overlays.obs_status as obs

        with patch.object(obs, "OBSWS_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                obs.OBSStatusOverlay()

    def test_poll_once_formats_scene_and_dropped(self):
        ov = _obs_overlay(show_dropped=True)
        ov._connect = lambda: _FakeOBSClient(scene="Main", skipped=2, render_skipped=3)
        lines = ov._poll_once()
        self.assertEqual(lines[0], "MAIN")
        self.assertEqual(lines[1], "DROP    5")  # 2 + 3 summed

    def test_poll_once_without_dropped(self):
        ov = _obs_overlay(show_dropped=False)
        ov._connect = lambda: _FakeOBSClient(scene="Cam")
        self.assertEqual(ov._poll_once(), ["CAM"])

    def test_worker_publishes_then_offline_on_failure(self):
        import threading

        ov = _obs_overlay()
        ov._connect = lambda: _FakeOBSClient(scene="Live")
        # One successful poll updates the cache.
        ov._poll_once = lambda: ["LIVE", "DROP    0"]
        stop = threading.Event()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                return ["LIVE", "DROP    0"]
            stop.set()  # end the loop after the 2nd pass
            raise OSError("connection dropped")

        ov._poll_once = flaky
        ov.poll_interval = 0.0
        with self.assertLogs("c64cast.overlays.obs_status", level="DEBUG"):
            ov._worker(stop)
        # Final state reflects the failure → OFFLINE banner, client dropped.
        self.assertEqual(ov.compute_strings(t=0.0), ["OBS OFFLINE"])
        self.assertIsNone(ov._client)

    def test_teardown_disconnects_client(self):
        ov = _obs_overlay()
        client = _FakeOBSClient()
        ov._client = client
        ov.teardown(api=MagicMock(), scene=MagicMock())
        self.assertTrue(client.disconnected)
        self.assertIsNone(ov._client)


if __name__ == "__main__":
    unittest.main()
