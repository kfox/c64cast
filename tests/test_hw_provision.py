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
