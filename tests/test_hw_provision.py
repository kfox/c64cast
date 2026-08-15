"""Tests for c64cast.hw.hw_provision — live U64 REU auto-provisioning + the
REST read-side helpers. No hardware: a minimal fake Ultimate64API records
put_config_item calls and serves canned REST config sections.

The sampler half (provision_sampler / sampler_is_available / wants_sampler)
is covered in tests/test_sampler.py alongside the rest of the Ultimate Audio
feature."""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest import mock

from c64cast.app import config as cfgmod
from c64cast.hw import hw_provision


def _write(path: str, body: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def _load(toml: str, suffix: str = ".toml") -> cfgmod.LoadResult:
    """Helper: write a single-system TOML to a tempfile, load via
    load_master, return the LoadResult."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "single" + suffix)
        _write(path, toml)
        return cfgmod.load_master(path)


class ReuIsEnabledHelperTest(unittest.TestCase):
    """hw_provision.reu_is_enabled() — the cli build_stack uses this to resolve
    the [video].use_reu_staged "auto" setting. True/False on a clean read, None
    on any failure or unrecognized shape (treated as "not available" upstream)."""

    def _api(self, *, json_value=None, get_side_effect=None):
        api = mock.MagicMock()
        api.base_url = "http://fake"
        if get_side_effect is not None:
            api.session.get.side_effect = get_side_effect
        else:
            resp = mock.MagicMock()
            resp.json.return_value = json_value
            resp.raise_for_status = mock.MagicMock()
            api.session.get.return_value = resp
        return api

    def _section(self, status):
        return {
            "C64 and Cartridge Settings": {"RAM Expansion Unit": status, "REU Size": "16 MB"},
            "errors": [],
        }

    def test_enabled_true(self):
        api = self._api(json_value=self._section("Enabled"))
        self.assertIs(hw_provision.reu_is_enabled(api), True)

    def test_disabled_false(self):
        api = self._api(json_value=self._section("Disabled"))
        self.assertIs(hw_provision.reu_is_enabled(api), False)

    def test_query_failure_none(self):
        import requests

        api = self._api(get_side_effect=requests.Timeout("read timeout"))
        self.assertIsNone(hw_provision.reu_is_enabled(api))

    def test_unrecognized_shape_none(self):
        api = self._api(json_value=["unexpected"])
        self.assertIsNone(hw_provision.reu_is_enabled(api))


class _FakeProfile:
    def __init__(self, supports_reu: bool = True) -> None:
        self.supports_reu = supports_reu


class _FakeApi:
    """Minimal stand-in for an Ultimate64API the REU provisioner needs:
    base_url + session.get for read_reu_config, a profile, and a recording
    put_config_item (which raises `put_error` if set, to exercise the
    best-effort path)."""

    def __init__(
        self,
        *,
        reu_status: str | None = "Enabled",
        reu_size: str | None = "16 MB",
        supports_reu: bool = True,
        put_error: Exception | None = None,
    ) -> None:
        self.base_url = "http://fake"
        self.profile = _FakeProfile(supports_reu)
        self.put_calls: list[tuple[str, str, str]] = []
        self._put_error = put_error
        self.session = mock.MagicMock()
        settings: dict[str, str] = {}
        if reu_status is not None:
            settings["RAM Expansion Unit"] = reu_status
        if reu_size is not None:
            settings["REU Size"] = reu_size
        resp = mock.MagicMock()
        resp.json.return_value = {"C64 and Cartridge Settings": settings, "errors": []}
        resp.raise_for_status = mock.MagicMock()
        self.session.get.return_value = resp

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        if self._put_error is not None:
            raise self._put_error
        self.put_calls.append((category, item, value))


def _cfg(toml: str) -> cfgmod.Config:
    return _load(toml).cfgs[0]


# A config that hard-requires the REU (use_reu_pump), with auto_reu defaulting
# on — the common provisioning trigger.
_PUMP_TOML = """
    [ultimate64]
    url = "http://fake"
    [audio]
    enabled = true
    use_reu_pump = true
    [[scenes]]
    type = "webcam"
    display = "petscii"
"""


class ProvisionReuTest(unittest.TestCase):
    """hw_provision.provision_reu() — auto-enable + size the REU (live,
    volatile) for runs that hard-require it, returning the originals for
    teardown restore."""

    def test_enables_and_sizes_a_disabled_reu(self):
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB")
        restore = hw_provision.provision_reu(api, _cfg(_PUMP_TOML))
        self.assertEqual(
            api.put_calls,
            [
                ("C64 and Cartridge Settings", "RAM Expansion Unit", "Enabled"),
                ("C64 and Cartridge Settings", "REU Size", "16 MB"),
            ],
        )
        # Restore must capture the ORIGINAL values, not the ones we set.
        self.assertEqual(restore, {"RAM Expansion Unit": "Disabled", "REU Size": "2 MB"})

    def test_noop_when_already_enabled_and_large(self):
        api = _FakeApi(reu_status="Enabled", reu_size="16 MB")
        restore = hw_provision.provision_reu(api, _cfg(_PUMP_TOML))
        self.assertEqual(api.put_calls, [])
        self.assertIsNone(restore)

    def test_grows_size_only_when_enabled_but_too_small(self):
        api = _FakeApi(reu_status="Enabled", reu_size="2 MB")
        restore = hw_provision.provision_reu(api, _cfg(_PUMP_TOML))
        self.assertEqual(api.put_calls, [("C64 and Cartridge Settings", "REU Size", "16 MB")])
        self.assertEqual(restore, {"REU Size": "2 MB"})

    def test_skipped_when_auto_reu_off(self):
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB")
        cfg = _cfg("""
            [ultimate64]
            url = "http://fake"
            auto_reu = false
            [audio]
            enabled = true
            use_reu_pump = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        self.assertIsNone(hw_provision.provision_reu(api, cfg))
        self.assertEqual(api.put_calls, [])

    def test_skipped_without_hard_opt_in(self):
        """use_reu_staged = "auto" is NOT a hard requirement (it self-heals to
        host-DMA double-buffer), so it must not trigger provisioning.

        backend = "dac" isolates this from the sampler path (which IS a hard
        REU reason — covered by ProvisionSamplerTest in test_sampler.py)."""
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB")
        cfg = _cfg("""
            [ultimate64]
            url = "http://fake"
            [audio]
            backend = "dac"
            [video]
            use_reu_staged = "auto"
            [[scenes]]
            type = "video"
            display = "mhires"
            file = "x.mp4"
        """)
        self.assertIsNone(hw_provision.provision_reu(api, cfg))
        self.assertEqual(api.put_calls, [])

    def test_skipped_on_no_reu_backend(self):
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB", supports_reu=False)
        self.assertIsNone(hw_provision.provision_reu(api, _cfg(_PUMP_TOML)))
        self.assertEqual(api.put_calls, [])

    def test_skipped_under_skip_probe(self):
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB")
        cfg = _cfg("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            use_reu_pump = true
            [debug]
            skip_probe = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        self.assertIsNone(hw_provision.provision_reu(api, cfg))
        self.assertEqual(api.put_calls, [])

    def test_best_effort_when_enable_put_fails(self):
        import requests

        api = _FakeApi(reu_status="Disabled", reu_size="2 MB", put_error=requests.Timeout("nope"))
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING"):
            restore = hw_provision.provision_reu(api, _cfg(_PUMP_TOML))
        # Enable PUT raised before anything stuck → nothing to restore.
        self.assertIsNone(restore)

    def test_best_effort_when_reu_state_unreadable(self):
        import requests

        api = _FakeApi()
        api.session.get.side_effect = requests.Timeout("read timeout")
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING"):
            restore = hw_provision.provision_reu(api, _cfg(_PUMP_TOML))
        self.assertIsNone(restore)
        self.assertEqual(api.put_calls, [])


class RestoreReuTest(unittest.TestCase):
    def test_restores_each_field(self):
        api = _FakeApi()
        hw_provision.restore_reu(api, {"RAM Expansion Unit": "Disabled", "REU Size": "2 MB"})
        self.assertEqual(
            api.put_calls,
            [
                ("C64 and Cartridge Settings", "RAM Expansion Unit", "Disabled"),
                ("C64 and Cartridge Settings", "REU Size", "2 MB"),
            ],
        )

    def test_noop_on_none(self):
        api = _FakeApi()
        hw_provision.restore_reu(api, None)
        self.assertEqual(api.put_calls, [])

    def test_best_effort_on_failure(self):
        import requests

        api = _FakeApi(put_error=requests.Timeout("nope"))
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING"):
            hw_provision.restore_reu(api, {"RAM Expansion Unit": "Disabled"})


class ReadReuConfigTest(unittest.TestCase):
    def test_reads_enabled_and_size(self):
        api = _FakeApi(reu_status="Enabled", reu_size="8 MB")
        self.assertEqual(hw_provision.read_reu_config(api), (True, "8 MB"))

    def test_disabled(self):
        api = _FakeApi(reu_status="Disabled", reu_size="2 MB")
        self.assertEqual(hw_provision.read_reu_config(api), (False, "2 MB"))

    def test_unreadable_returns_none_pair(self):
        import requests

        api = _FakeApi()
        api.session.get.side_effect = requests.Timeout("read timeout")
        self.assertEqual(hw_provision.read_reu_config(api), (None, None))


if __name__ == "__main__":
    unittest.main()


class _FakeVideoApi:
    """A fake Ultimate serving the "U64 Specific Settings" category and
    recording put_config_item calls, for the video-output provisioner."""

    def __init__(
        self,
        *,
        system_mode: str | None = "NTSC",
        scan_resolution: str | None = "SD (480p/576p)",
        palette_definition: str | None = "",
        supports_system_mode: bool = True,
        put_error: Exception | None = None,
    ) -> None:
        from c64cast.hw.backend import HardwareProfile

        self.base_url = "http://fake"
        self.profile = HardwareProfile(
            name="Fake", family="fake", supports_system_mode=supports_system_mode
        )
        self.put_calls: list[tuple[str, str, str]] = []
        self._put_error = put_error
        self.session = mock.MagicMock()
        settings: dict[str, str] = {}
        if system_mode is not None:
            settings["System Mode"] = system_mode
        if scan_resolution is not None:
            settings["HDMI Scan Resolution"] = scan_resolution
        if palette_definition is not None:
            settings["Palette Definition"] = palette_definition
        resp = mock.MagicMock()
        resp.json.return_value = {"U64 Specific Settings": settings, "errors": []}
        resp.raise_for_status = mock.MagicMock()
        self.session.get.return_value = resp

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        if self._put_error is not None:
            raise self._put_error
        self.put_calls.append((category, item, value))


def _video_cfg(system: str = "PAL", **overrides: str) -> cfgmod.Config:
    lines = [f'{k} = "{v}"' for k, v in overrides.items()]
    return _cfg(
        f'''
        [ultimate64]
        url = "http://fake"
        system = "{system}"
        {chr(10).join(lines)}
        [[scenes]]
        type = "webcam"
        display = "petscii"
    '''
    )


_CAT = "U64 Specific Settings"


class SystemModeTimingTest(unittest.TestCase):
    """The System Mode label -> machine timing map. The suffix selects the
    timing and the prefix only the analog chroma, so the table looks backwards
    — these pin it so nobody "corrects" it."""

    def test_every_firmware_label_is_mapped(self):
        # From the firmware's color_sel[] (u64_config.cc). A new label mapping
        # to nothing would silently make read_system_timing return None.
        self.assertEqual(
            set(hw_provision.SYSTEM_MODE_TIMING),
            {"PAL", "NTSC", "PAL-60", "NTSC-50", "PAL-60/L", "NTSC-50/L"},
        )

    def test_suffix_selects_timing_not_prefix(self):
        for label in ("PAL", "NTSC-50", "NTSC-50/L"):
            self.assertEqual(hw_provision.SYSTEM_MODE_TIMING[label], "PAL", label)
        for label in ("NTSC", "PAL-60", "PAL-60/L"):
            self.assertEqual(hw_provision.SYSTEM_MODE_TIMING[label], "NTSC", label)

    def test_mode_for_round_trips_through_timing(self):
        for (timing, _chroma), label in hw_provision.SYSTEM_MODE_FOR.items():
            self.assertEqual(hw_provision.SYSTEM_MODE_TIMING[label], timing)

    def test_read_system_timing_maps_the_live_label(self):
        self.assertEqual(
            hw_provision.read_system_timing(_FakeVideoApi(system_mode="NTSC-50")), "PAL"
        )

    def test_read_system_timing_none_on_unknown_label(self):
        self.assertIsNone(hw_provision.read_system_timing(_FakeVideoApi(system_mode="SECAM")))

    def test_read_video_output_reports_both_fields(self):
        api = _FakeVideoApi(system_mode="PAL", scan_resolution="HD (720p)")
        self.assertEqual(hw_provision.read_video_output(api), ("PAL", "HD (720p)"))

    def test_read_video_output_tolerates_a_board_without_the_upscaler(self):
        # Older U64 firmware doesn't register "HDMI Scan Resolution" at all.
        api = _FakeVideoApi(system_mode="PAL", scan_resolution=None)
        self.assertEqual(hw_provision.read_video_output(api), ("PAL", None))


class ProvisionVideoOutputTest(unittest.TestCase):
    """hw_provision.provision_video_output() — the opt-in System Mode retime
    plus the HDMI upscaler that keeps capture working across it."""

    def test_noop_when_both_settings_are_default(self):
        api = _FakeVideoApi(system_mode="NTSC")
        self.assertIsNone(hw_provision.provision_video_output(api, _video_cfg("PAL")))
        self.assertEqual(api.put_calls, [])

    def test_switches_timing_and_raises_sd_to_hd(self):
        api = _FakeVideoApi(system_mode="NTSC", scan_resolution="SD (480p/576p)")
        restore = hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        self.assertEqual(
            api.put_calls,
            [
                (_CAT, "System Mode", "NTSC-50"),
                (_CAT, "HDMI Scan Resolution", "HD (720p)"),
            ],
        )
        self.assertEqual(
            restore,
            {"System Mode": "NTSC", "HDMI Scan Resolution": "SD (480p/576p)"},
        )

    def test_auto_leaves_scan_resolution_alone_when_it_is_not_sd(self):
        api = _FakeVideoApi(system_mode="NTSC", scan_resolution="FullHD (1080p)")
        restore = hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "NTSC-50")])
        self.assertEqual(restore, {"System Mode": "NTSC"})

    def test_auto_does_not_touch_a_machine_it_did_not_retime(self):
        # Already PAL-timed: nothing to fix, so SD stays SD even though this
        # machine would capture better at HD. We only clean up our own change.
        api = _FakeVideoApi(system_mode="PAL", scan_resolution="SD (480p/576p)")
        self.assertIsNone(
            hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        )
        self.assertEqual(api.put_calls, [])

    def test_explicit_scan_resolution_applies_without_a_retime(self):
        api = _FakeVideoApi(system_mode="NTSC", scan_resolution="SD (480p/576p)")
        restore = hw_provision.provision_video_output(
            api, _video_cfg("NTSC", hdmi_scan_resolution="FullHD (1080p)")
        )
        self.assertEqual(api.put_calls, [(_CAT, "HDMI Scan Resolution", "FullHD (1080p)")])
        self.assertEqual(restore, {"HDMI Scan Resolution": "SD (480p/576p)"})

    def test_keep_never_touches_the_upscaler(self):
        api = _FakeVideoApi(system_mode="NTSC", scan_resolution="SD (480p/576p)")
        restore = hw_provision.provision_video_output(
            api, _video_cfg("PAL", sid_video_mode="auto", hdmi_scan_resolution="keep")
        )
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "NTSC-50")])
        self.assertEqual(restore, {"System Mode": "NTSC"})

    def test_keeps_the_analog_chroma_encoding_the_machine_is_set_for(self):
        # An NTSC-chroma machine retimed to PAL becomes NTSC-50, not PAL, and a
        # PAL-chroma one retimed to NTSC becomes PAL-60: over HDMI they're the
        # same picture either way, but a composite user chose that encoding.
        api = _FakeVideoApi(system_mode="NTSC", scan_resolution=None)
        hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "NTSC-50")])
        api = _FakeVideoApi(system_mode="PAL", scan_resolution=None)
        hw_provision.provision_video_output(api, _video_cfg("NTSC", sid_video_mode="auto"))
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "PAL-60")])

    def test_a_locked_hybrid_retimes_to_the_plain_standard_mode(self):
        # Every /L mode is a hybrid, so retiming one always lands on the OTHER
        # standard's plain mode — which has no locked form. A composite user
        # who chose /L for its subcarrier accuracy loses it until teardown;
        # pinned here because it looks like an oversight and isn't.
        api = _FakeVideoApi(system_mode="NTSC-50/L", scan_resolution=None)
        hw_provision.provision_video_output(api, _video_cfg("NTSC", sid_video_mode="auto"))
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "NTSC")])
        api = _FakeVideoApi(system_mode="PAL-60/L", scan_resolution=None)
        hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        self.assertEqual(api.put_calls, [(_CAT, "System Mode", "PAL")])

    def test_noop_when_timing_already_matches(self):
        api = _FakeVideoApi(system_mode="NTSC-50", scan_resolution=None)
        self.assertIsNone(
            hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        )
        self.assertEqual(api.put_calls, [])

    def test_skipped_on_a_backend_without_the_category(self):
        api = _FakeVideoApi(supports_system_mode=False)
        self.assertIsNone(
            hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        )
        self.assertEqual(api.put_calls, [])

    def test_skipped_under_skip_probe(self):
        cfg = _video_cfg("PAL", sid_video_mode="auto")
        cfg.debug.skip_probe = True
        api = _FakeVideoApi()
        self.assertIsNone(hw_provision.provision_video_output(api, cfg))
        self.assertEqual(api.put_calls, [])

    def test_unreadable_mode_leaves_the_machine_alone(self):
        api = _FakeVideoApi(system_mode=None, scan_resolution="SD (480p/576p)")
        self.assertIsNone(
            hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        )
        self.assertEqual(api.put_calls, [])

    def test_a_failed_put_degrades_instead_of_raising(self):
        import requests

        api = _FakeVideoApi(put_error=requests.RequestException("boom"))
        self.assertIsNone(
            hw_provision.provision_video_output(api, _video_cfg("PAL", sid_video_mode="auto"))
        )


class RestoreVideoOutputTest(unittest.TestCase):
    def test_puts_every_field_back(self):
        api = _FakeVideoApi()
        hw_provision.restore_video_output(
            api, {"System Mode": "NTSC", "HDMI Scan Resolution": "SD (480p/576p)"}
        )
        self.assertEqual(
            api.put_calls,
            [
                (_CAT, "System Mode", "NTSC"),
                (_CAT, "HDMI Scan Resolution", "SD (480p/576p)"),
            ],
        )

    def test_noop_on_none(self):
        api = _FakeVideoApi()
        hw_provision.restore_video_output(api, None)
        self.assertEqual(api.put_calls, [])

    def test_a_failed_restore_only_logs(self):
        import requests

        api = _FakeVideoApi(put_error=requests.RequestException("boom"))
        hw_provision.restore_video_output(api, {"System Mode": "NTSC"})


class ResolveSystemTest(unittest.TestCase):
    """hw_provision.resolve_system() — settles system = "auto" against the
    live machine and re-folds the profile fields derived from it."""

    def _api(self, **kw):
        return _FakeVideoApi(**kw)

    def test_auto_takes_the_machines_timing(self):
        api = self._api(system_mode="NTSC-50")
        cfg = _video_cfg("auto")
        hw_provision.resolve_system(cfg, api)
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertEqual(api.profile.system, "PAL")
        self.assertEqual(api.profile.default_fps, 50.0)

    def test_auto_falls_back_to_ntsc_when_unreadable(self):
        api = self._api(supports_system_mode=False)
        cfg = _video_cfg("auto")
        hw_provision.resolve_system(cfg, api)
        self.assertEqual(cfg.ultimate64.system, "NTSC")
        self.assertEqual(api.profile.system, "NTSC")

    def test_auto_falls_back_to_ntsc_under_skip_probe(self):
        api = self._api(system_mode="PAL")
        cfg = _video_cfg("auto")
        cfg.debug.skip_probe = True
        hw_provision.resolve_system(cfg, api)
        self.assertEqual(cfg.ultimate64.system, "NTSC")

    def test_an_explicit_value_wins_over_the_machine(self):
        api = self._api(system_mode="PAL")
        cfg = _video_cfg("NTSC")
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING") as logs:
            hw_provision.resolve_system(cfg, api)
        self.assertEqual(cfg.ultimate64.system, "NTSC")
        self.assertIn("PAL timing", "".join(logs.output))

    def test_an_agreeing_explicit_value_is_quiet(self):
        api = self._api(system_mode="NTSC-50")
        cfg = _video_cfg("PAL")
        hw_provision.resolve_system(cfg, api)
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertEqual(api.profile.system, "PAL")


def _palette_cfg(host_palette: str = "auto") -> cfgmod.Config:
    return _cfg(
        f'''
        [ultimate64]
        url = "http://fake"
        system = "NTSC"
        [hardware]
        host_palette = "{host_palette}"
        [[scenes]]
        type = "webcam"
        display = "hires"
    '''
    )


class ResolvePaletteTest(unittest.TestCase):
    """hw_provision.resolve_palette() — settles host_palette = "auto" against
    the machine and points the color pipeline at what it emits."""

    def setUp(self):
        from c64cast.video import palette as pal

        # The active palette is process-wide, so every case here has to put it
        # back or it leaks into whatever test runs next.
        before = pal.C64_PALETTE_BGR.copy(), pal.active_host_palette_name()
        self.addCleanup(lambda: pal.set_host_palette(before[0], name=before[1]))
        hw_provision._palette_resolved = False
        self.addCleanup(setattr, hw_provision, "_palette_resolved", False)
        self.pal = pal

    def test_auto_takes_the_ultimates_own_table(self):
        hw_provision.resolve_palette(_palette_cfg(), _FakeVideoApi())
        self.assertEqual(self.pal.active_host_palette_name(), "u64")
        self.assertEqual(tuple(self.pal.C64_PALETTE_BGR[8]), (32.0, 78.0, 152.0))

    def test_auto_assumes_a_real_vic_off_an_ultimate(self):
        hw_provision.resolve_palette(_palette_cfg(), _FakeVideoApi(supports_system_mode=False))
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_auto_assumes_a_real_vic_under_skip_probe(self):
        cfg = _palette_cfg()
        cfg.debug.skip_probe = True
        hw_provision.resolve_palette(cfg, _FakeVideoApi())
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_an_explicit_value_wins_over_the_machine(self):
        hw_provision.resolve_palette(_palette_cfg("pepto"), _FakeVideoApi())
        self.assertEqual(self.pal.active_host_palette_name(), "pepto")

    def test_a_custom_vpl_on_the_machine_warns(self):
        api = _FakeVideoApi(palette_definition="mine.vpl")
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING") as logs:
            hw_provision.resolve_palette(_palette_cfg(), api)
        joined = "".join(logs.output)
        self.assertIn("mine.vpl", joined)
        # Falls back to the firmware table rather than refusing to run.
        self.assertEqual(self.pal.active_host_palette_name(), "u64")

    def test_an_ensemble_disagreement_warns_and_keeps_the_first(self):
        hw_provision.resolve_palette(_palette_cfg(), _FakeVideoApi())
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING") as logs:
            hw_provision.resolve_palette(_palette_cfg("pepto"), _FakeVideoApi())
        self.assertIn("differs from the one already in effect", "".join(logs.output))
        self.assertEqual(self.pal.active_host_palette_name(), "u64")

    def test_an_ensemble_agreement_is_quiet(self):
        api = _FakeVideoApi()
        hw_provision.resolve_palette(_palette_cfg(), api)
        with self.assertNoLogs("c64cast.hw.hw_provision", level="WARNING"):
            hw_provision.resolve_palette(_palette_cfg(), api)
