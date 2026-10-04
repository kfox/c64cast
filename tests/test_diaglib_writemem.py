"""The diag tools' REST memory write: the form every firmware accepts, and a
failure that raises instead of drawing nothing."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import requests

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _DIAGS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_diaglib() -> ModuleType:
    """scripts/diags/ is not a package; load _diaglib by path without leaving
    it, or its sys.path insert, behind in the worker."""
    with patch.object(sys, "path", list(sys.path)), patch.dict(sys.modules):
        return _load("_diaglib")


_diaglib = _load_diaglib()


def _response(status: int, text: str = "") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text
    return r


class RestWritememTests(unittest.TestCase):
    def test_sends_put_with_address_and_hex_data(self):
        with patch.object(_diaglib, "rest_request", return_value=_response(200)) as req:
            _diaglib.rest_writemem(0xD020, b"\x00\x06", "http://u64", timeout=1.5)
        req.assert_called_once_with(
            "PUT",
            "http://u64/v1/machine:writemem",
            params={"address": "D020", "data": "0006"},
            timeout=1.5,
        )

    def test_firmware_refusal_raises_with_its_text(self):
        refusal = _response(412, '{"errors": ["Expected Body, but got none."]}')
        with (
            patch.object(_diaglib, "rest_request", return_value=refusal),
            self.assertRaisesRegex(requests.HTTPError, "HTTP 412.*Expected Body"),
        ):
            _diaglib.rest_writemem(0xD020, b"\x01", "http://u64")

    def test_flash_border_raises_on_refusal(self):
        with (
            patch.object(_diaglib, "rest_request", return_value=_response(403, "Forbidden.")),
            self.assertRaises(requests.HTTPError),
        ):
            _diaglib.flash_border("http://u64", 1)

    def test_out_of_range_writes_refused_before_sending(self):
        cases = {
            "empty": (0xD020, b""),
            "past the URL form's limit": (0x0400, bytes(_diaglib.REST_WRITEMEM_MAX + 1)),
            "past $FFFF": (0xFFFF, b"\x00\x00"),
        }
        for label, (address, data) in cases.items():
            with (
                self.subTest(label),
                patch.object(_diaglib, "rest_request") as req,
                self.assertRaises(ValueError),
            ):
                _diaglib.rest_writemem(address, data, "http://u64")
            req.assert_not_called()

    def test_largest_url_form_write_is_sent(self):
        with patch.object(_diaglib, "rest_request", return_value=_response(200)) as req:
            _diaglib.rest_writemem(0xFF80, bytes(_diaglib.REST_WRITEMEM_MAX), "http://u64")
        req.assert_called_once()


if __name__ == "__main__":
    unittest.main()
