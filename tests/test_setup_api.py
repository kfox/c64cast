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

    def test_write_connection_keeps_the_secrets_the_file_already_carried(self):
        # The critical defect this unit closed. `_write_connection` seeds a
        # Config from the machine layer (secrets included) and rewrites the
        # same file; `config_serialize.dumps` suppresses every SECRET_FIELDS
        # value, so the rewrite used to come back without the `dma_password` a
        # password-protected U64 needs and without the `[web] token` pin
        # `token_settable` exists to protect — silently, on an appliance whose
        # POST is unauthenticated while the setup window is open.
        from c64cast.app.connect import parse_connection_uri
        from c64cast.control.setup_api import _write_connection

        paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
        paths.settings_path().write_text(
            '[ultimate64]\ndma_password = "hunter2"\n\n[web]\ntoken = "a-pinned-web-token"\n',
            encoding="utf-8",
        )

        # Not the default host, or `minimal=True` would have nothing to write.
        _write_connection(parse_connection_uri("u64://10.0.0.9"))

        text = paths.settings_path().read_text(encoding="utf-8")
        self.assertIn('dma_password = "hunter2"', text)
        self.assertIn('token = "a-pinned-web-token"', text)
        self.assertIn("10.0.0.9", text)

    def test_write_token_persists_0600(self):
        from c64cast.control.setup_api import _write_token

        _write_token("a-chosen-token-value")
        path = paths.web_token_path()
        self.assertEqual(path.read_text().strip(), "a-chosen-token-value")
        # Windows has no POSIX mode bits — `chmod` there only toggles the
        # read-only flag, and the file reads back 0o666 — so the same
        # POSIX-only assertion `test_web_api` makes about `serve`'s generated
        # token is made here about the one this form writes.
        if os.name != "nt":
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

    def test_an_oversized_body_is_refused_before_it_is_buffered(self):
        # Unauthenticated while the window is open, so `request.json()`'s
        # unbounded accumulation was a remote memory exhaustion on a box with
        # 1-2 GB — taking down a process that owns live hardware.
        from c64cast.control import auth

        client = self._client()
        with mock.patch.object(auth, "MAX_BODY_BYTES", 64):
            resp = client.post("/api/setup", content=b"x" * 512)
        self.assertEqual(resp.status_code, 413)
        # The cap it tripped is operator diagnostics, not something an
        # unauthenticated caller is told.
        self.assertEqual(resp.json()["error"], auth.BODY_TOO_LARGE_ERROR)
        self.assertNotIn("64", resp.json()["error"])
        self.assertFalse(paths.setup_state_path().is_file())

    def test_a_whitespace_padded_token_round_trips_to_the_link_it_hands_back(self):
        # `_write_token` persisted what it was given and `serve._generated_token`
        # reads that file back stripped, so a token pasted with a trailing
        # space went out in `login_url` with the space and came back after the
        # restart without it: the one link an appliance admin was handed
        # answered 401 forever, with no other way to learn the real token.
        chosen = "z" * MIN_TOKEN_LENGTH
        client = self._client()
        body = client.post(
            "/api/setup", json={"connection": "u64://192.168.2.64", "token": f"  {chosen}\t"}
        ).json()
        self.assertEqual(paths.web_token_path().read_text().strip(), chosen)
        self.assertEqual(body["login_url"], login_url(chosen))

    def test_a_whitespace_only_token_keeps_the_hosts_own(self):
        # 16 spaces passed MIN_TOKEN_LENGTH, stripped to "" on read, and made
        # the host mint a brand-new credential nobody had ever seen — with the
        # setup window already closed by the completion marker, so recovery
        # needed shell access or a reflash.
        client = self._client(token="the-current-token")
        resp = client.post(
            "/api/setup", json={"connection": "u64://192.168.2.64", "token": " " * 20}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(paths.web_token_path().is_file())
        self.assertEqual(resp.json()["login_url"], login_url("the-current-token"))

    def test_a_multiline_token_is_refused(self):
        # `_write_token` appends its own newline, so the file's shape would be
        # ambiguous.
        client = self._client()
        resp = client.post(
            "/api/setup",
            json={"connection": "u64://192.168.2.64", "token": "a" * 10 + "\n" + "b" * 10},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("single line", resp.json()["error"])
        self.assertFalse(paths.setup_state_path().is_file())

    def test_a_failed_write_answers_with_the_path_and_leaves_setup_pending(self):
        # The admin's only interface is this form, so an OSError used to leave
        # them with FastAPI's bare 500 and no next step.
        client = self._client()
        boom = OSError(13, "Permission denied")
        boom.filename = "/nowhere/settings.toml"
        with mock.patch("c64cast.control.setup_api._write_connection", side_effect=boom):
            with self.assertLogs("c64cast.control.setup_api", level="ERROR"):
                resp = client.post("/api/setup", json={"connection": "u64://192.168.2.64"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("/nowhere/settings.toml", resp.json()["error"])
        self.assertIn("Permission denied", resp.json()["error"])
        self.assertFalse(paths.setup_state_path().is_file())
        self.assertEqual(self.completed, [])

    def test_the_connection_lands_before_the_token_is_replaced(self):
        # Reordered: the token used to be written first, so a failure writing
        # the connection left the host's credential already replaced by one
        # the 500 never handed back.
        client = self._client()
        with mock.patch(
            "c64cast.control.setup_api._write_connection", side_effect=OSError("no disk")
        ):
            with self.assertLogs("c64cast.control.setup_api", level="ERROR"):
                client.post(
                    "/api/setup",
                    json={
                        "connection": "u64://192.168.2.64",
                        "token": "q" * MIN_TOKEN_LENGTH,
                    },
                )
        self.assertFalse(paths.web_token_path().is_file())


if __name__ == "__main__":
    unittest.main()
