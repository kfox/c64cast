"""The diag tools' REST config read and write: a body that does not decode, or
decodes to something other than an object, is a failed call rather than a
raise."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import requests
from _fakes import TOO_DEEP_JSON

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


def _load_diaglib() -> ModuleType:
    """scripts/diags/ is not a package; load _diaglib by path without leaving
    it, or its sys.path insert, behind in the worker."""
    with patch.object(sys, "path", list(sys.path)), patch.dict(sys.modules):
        spec = importlib.util.spec_from_file_location("_diaglib", _DIAGS / "_diaglib.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["_diaglib"] = module
        spec.loader.exec_module(module)
        return module


_diaglib = _load_diaglib()


def _response(body: bytes) -> requests.Response:
    r = requests.Response()
    r.status_code = 200
    r._content = body
    r.encoding = "utf-8"
    return r


_UNUSABLE_BODIES = {
    "too deep": TOO_DEEP_JSON,
    "not JSON": b"<html></html>",
    "a list": b"[1, 2]",
}


class RestConfigTests(unittest.TestCase):
    def test_get_reads_the_inner_settings(self):
        body = b'{"SID Sockets Configuration": {"SID Socket 1": "Enabled"}, "errors": []}'
        with patch.object(_diaglib, "rest_request", return_value=_response(body)):
            got = _diaglib.rest_get_config("SID Sockets Configuration", "http://u64")
        self.assertEqual(got, {"SID Socket 1": "Enabled"})

    def test_get_of_an_unusable_body_is_none(self):
        for name, body in _UNUSABLE_BODIES.items():
            with (
                self.subTest(body=name),
                patch.object(_diaglib, "rest_request", return_value=_response(body)),
            ):
                self.assertIsNone(_diaglib.rest_get_config("Audio Mixer", "http://u64"))

    def test_set_is_true_only_for_an_empty_errors_list(self):
        for body, expected in ((b'{"errors": []}', True), (b'{"errors": ["no"]}', False)):
            with (
                self.subTest(body=body),
                patch.object(_diaglib, "rest_request", return_value=_response(body)),
            ):
                self.assertIs(
                    _diaglib.rest_set_config("Audio Mixer", "x", "1", "http://u64"), expected
                )

    def test_set_with_an_unusable_body_is_false(self):
        for name, body in _UNUSABLE_BODIES.items():
            with (
                self.subTest(body=name),
                patch.object(_diaglib, "rest_request", return_value=_response(body)),
            ):
                self.assertFalse(_diaglib.rest_set_config("Audio Mixer", "x", "1", "http://u64"))


if __name__ == "__main__":
    unittest.main()
