"""Tests for the appliance setup form's API.

`WriteHelpersTest` drives the file-writing helpers directly against a
temporary data/config root. `RouteTest` builds a small FastAPI app, registers
the routes through `register_setup_routes`, and drives it with TestClient —
mirroring `test_control_auth.py`'s split between token-plumbing and
end-to-end tests."""

# pyright: reportAttributeAccessIssue=false, reportOptionalCall=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
import warnings
from unittest import mock

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]

from c64cast.app import config as cfgmod
from c64cast.app import paths
from c64cast.control.setup_api import MIN_TOKEN_LENGTH, login_url, register_setup_routes


class _TmpRootsTestCase(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        self.config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_dir.cleanup)
        self.addCleanup(self.config_dir.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"C64CAST_DATA_DIR": self.data_dir.name, "C64CAST_SETTINGS": self._settings_path()},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings_path(self) -> str:
        return os.path.join(self.config_dir.name, "settings.toml")


class WriteHelpersTest(_TmpRootsTestCase):
    def test_write_connection_lands_in_machine_settings(self):
        from c64cast.app.connect import parse_connection_uri
        from c64cast.control.setup_api import _write_connection

        _write_connection(parse_connection_uri("u64://192.168.2.64"))
        cfg = cfgmod.Config()
        cfgmod.apply_machine_settings(cfg)
        self.assertEqual(cfg.ultimate64.url, "http://192.168.2.64")

    def test_write_connection_merges_with_an_existing_setting(self):
        from c64cast.app.connect import parse_connection_uri
        from c64cast.control.setup_api import _write_connection

        cfg = cfgmod.Config()
        cfg.video.device = "3"
        from c64cast.app import config_serialize

        paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
        paths.settings_path().write_text(
            config_serialize.dumps(cfg, minimal=True, schema_path=None)
        )

        _write_connection(parse_connection_uri("tr://"))
        merged = cfgmod.Config()
        cfgmod.apply_machine_settings(merged)
        self.assertEqual(merged.hardware.backend, "teensyrom")
        self.assertEqual(merged.video.device, "3")

    def test_write_token_persists_0600(self):
        from c64cast.control.setup_api import _write_token

        _write_token("a-chosen-token-value")
        path = paths.web_token_path()
        self.assertEqual(path.read_text().strip(), "a-chosen-token-value")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mark_complete_writes_after_the_fact(self):
        from c64cast.control.setup_api import _mark_complete

        self.assertFalse(paths.setup_state_path().is_file())
        _mark_complete("u64://192.168.2.64")
        data = json.loads(paths.setup_state_path().read_text())
        self.assertEqual(data["connection"], "u64://192.168.2.64")
        self.assertIn("completed_at", data)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class RouteTest(_TmpRootsTestCase):
    def _client(self, *, token: str = "the-current-token", settable: bool = True):
        from fastapi import FastAPI

        app = FastAPI()
        self.completed = []
        register_setup_routes(
            app,
            token=token,
            token_settable=settable,
            on_complete=lambda: self.completed.append(True),
        )
        return TestClient(app)

    def test_get_reports_pending_and_never_the_token(self):
        resp = self._client(token="tok-abc").get("/api/setup")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["pending"])
        self.assertTrue(body["token_settable"])
        self.assertNotIn("tok-abc", resp.text)

    def test_get_reports_a_token_it_cannot_change(self):
        body = self._client(settable=False).get("/api/setup").json()
        self.assertFalse(body["token_settable"])

    def test_post_with_a_valid_connection_completes_setup(self):
        client = self._client()
        resp = client.post("/api/setup", json={"connection": "u64://192.168.2.64"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertTrue(paths.setup_state_path().is_file())
        self.assertEqual(self.completed, [True])

    def test_post_hands_back_a_login_url_carrying_the_token(self):
        client = self._client(token="the-current-token")
        body = client.post("/api/setup", json={"connection": "u64://192.168.2.64"}).json()
        self.assertEqual(body["login_url"], login_url("the-current-token"))
        self.assertIn("token=the-current-token", body["login_url"])

    def test_post_hands_back_a_login_url_carrying_a_chosen_token(self):
        chosen = "y" * MIN_TOKEN_LENGTH
        client = self._client()
        body = client.post(
            "/api/setup", json={"connection": "u64://192.168.2.64", "token": chosen}
        ).json()
        self.assertEqual(body["login_url"], login_url(chosen))

    def test_post_refuses_a_token_the_host_would_ignore(self):
        client = self._client(settable=False)
        resp = client.post(
            "/api/setup",
            json={"connection": "u64://192.168.2.64", "token": "z" * MIN_TOKEN_LENGTH},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fixed by its configuration", resp.json()["error"])
        self.assertFalse(paths.setup_state_path().is_file())
        self.assertFalse(paths.web_token_path().is_file())

    def test_post_without_a_token_still_completes_when_one_is_fixed(self):
        client = self._client(settable=False)
        resp = client.post("/api/setup", json={"connection": "u64://192.168.2.64"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(paths.setup_state_path().is_file())

    def test_post_with_a_bad_connection_is_refused_and_does_not_complete(self):
        client = self._client()
        resp = client.post("/api/setup", json={"connection": "not-a-real-scheme://x"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(paths.setup_state_path().is_file())
        self.assertEqual(self.completed, [])

    def test_post_with_no_connection_is_refused(self):
        client = self._client()
        resp = client.post("/api/setup", json={})
        self.assertEqual(resp.status_code, 400)

    def test_post_with_a_short_custom_token_is_refused(self):
        client = self._client()
        resp = client.post(
            "/api/setup", json={"connection": "u64://192.168.2.64", "token": "short"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(paths.setup_state_path().is_file())

    def test_post_with_a_custom_token_replaces_the_generated_one(self):
        client = self._client()
        chosen = "x" * MIN_TOKEN_LENGTH
        resp = client.post("/api/setup", json={"connection": "u64://192.168.2.64", "token": chosen})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(paths.web_token_path().read_text().strip(), chosen)

    def test_post_keeps_the_existing_token_when_none_is_chosen(self):
        client = self._client()
        client.post("/api/setup", json={"connection": "u64://192.168.2.64"})
        self.assertFalse(paths.web_token_path().is_file())

    def test_a_malformed_body_is_refused(self):
        client = self._client()
        resp = client.post("/api/setup", content=b"not json")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
