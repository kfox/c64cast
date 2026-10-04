"""`c64cast._json.decode_json`: every undecodable body raises the one type
that both a `ValueError` handler and a `requests.RequestException` handler
catch, including a body nested too deeply for the decoder."""

from __future__ import annotations

import json
import unittest

import requests
from _fakes import TOO_DEEP_JSON

from c64cast._json import decode_json


def _response(body: bytes) -> requests.Response:
    r = requests.Response()
    r.status_code = 200
    r._content = body
    r.encoding = "utf-8"
    return r


class DecodeJsonTest(unittest.TestCase):
    def test_the_deep_bodies_do_overflow_the_decoder(self):
        for body in (TOO_DEEP_JSON, b'{"a":' * 2_000_000):
            with self.assertRaises(RecursionError):
                json.loads(body)

    def test_decodes_a_document_and_a_response(self):
        for source in (
            b'{"a": [1]}',
            '{"a": [1]}',
            bytearray(b'{"a": [1]}'),
            _response(b'{"a": [1]}'),
        ):
            with self.subTest(source=type(source).__name__):
                self.assertEqual(decode_json(source), {"a": [1]})

    def test_every_undecodable_body_raises_one_type_both_handlers_catch(self):
        bodies = {
            "too deep": TOO_DEEP_JSON,
            "too deep object": b'{"a":' * 2_000_000,
            "malformed": b"<html>not json</html>",
            "empty": b"",
        }
        for name, body in bodies.items():
            for source in (body, _response(body)):
                with self.subTest(body=name, source=type(source).__name__):
                    with self.assertRaises(requests.exceptions.JSONDecodeError) as cm:
                        decode_json(source)
                    self.assertIsInstance(cm.exception, ValueError)
                    self.assertIsInstance(cm.exception, requests.RequestException)

    def test_a_document_that_is_not_utf_8_raises_the_same_type(self):
        # A Response decodes its text with replacement characters instead.
        with self.assertRaises(requests.exceptions.JSONDecodeError):
            decode_json(b'"\xff\xfe"')

    def test_a_malformed_document_keeps_the_decoders_position(self):
        with self.assertRaises(requests.exceptions.JSONDecodeError) as cm:
            decode_json('{"a": }')
        self.assertEqual(cm.exception.pos, 6)


if __name__ == "__main__":
    unittest.main()
