"""Tests for the JSON-schema generator + committed schema file.

Guards:
  * the committed c64cast.schema.json matches a fresh `build_schema()`
    (so `make schema` was run after a config change), and
  * the real example configs all validate against it (so the schema isn't
    accidentally over-strict and breaking editor autocomplete).
"""
from __future__ import annotations

import glob
import json
import os
import tomllib
import unittest

from c64cast import schema

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMMITTED = os.path.join(_REPO_ROOT, "c64cast.schema.json")


class SchemaBuildTest(unittest.TestCase):
    def test_committed_schema_is_fresh(self):
        with open(_COMMITTED, encoding="utf-8") as f:
            committed = json.load(f)
        fresh = schema.build_schema()
        self.assertEqual(
            committed, fresh,
            "c64cast.schema.json is stale — run `make schema` to regenerate.")

    def test_top_level_shape(self):
        s = schema.build_schema()
        self.assertEqual(s["additionalProperties"], False)
        for key in ("ultimate64", "audio", "scenes", "playlist"):
            self.assertIn(key, s["properties"])
        self.assertEqual(s["properties"]["scenes"]["type"], "array")


class SchemaValidatesExamplesTest(unittest.TestCase):
    def setUp(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema not installed (dev dependency)")
        self.validator = Draft202012Validator(schema.build_schema())

    def _configs(self):
        yield os.path.join(_REPO_ROOT, "config", "c64cast.example.toml")
        yield from sorted(glob.glob(
            os.path.join(_REPO_ROOT, "config", "examples", "*.toml")))

    def test_examples_validate(self):
        for path in self._configs():
            with self.subTest(config=os.path.relpath(path, _REPO_ROOT)):
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                errors = sorted(self.validator.iter_errors(data),
                                key=lambda e: list(e.path))
                if errors:
                    msg = "\n".join(
                        f"  {'/'.join(map(str, e.path))}: {e.message}"
                        for e in errors[:10])
                    self.fail(f"{path} failed schema validation:\n{msg}")

    def test_typo_is_rejected(self):
        # A bogus top-level key should fail (additionalProperties: false).
        bad = {"audio": {"enabledd": True}}
        self.assertTrue(list(self.validator.iter_errors(bad)))


if __name__ == "__main__":
    unittest.main()
