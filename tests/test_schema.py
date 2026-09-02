"""Tests for the JSON-schema generator + committed schema file.

Guards:
  * the committed c64cast/data/c64cast.schema.json matches a fresh
    `build_schema()` (so `make schema` was run after a config change), and
  * the real example configs all validate against it (so the schema isn't
    accidentally over-strict and breaking editor autocomplete).

Both the schema and the examples are read through `paths`, the same resolver
the CLI uses, so these run against a checkout and an installed package alike.
"""

from __future__ import annotations

import json
import tomllib
import unittest

from c64cast.app import introspect, paths, schema

_COMMITTED = paths.packaged_schema_path()


class SchemaBuildTest(unittest.TestCase):
    def test_committed_schema_is_fresh(self):
        with open(_COMMITTED, encoding="utf-8") as f:
            committed = json.load(f)
        fresh = schema.build_schema()
        self.assertEqual(
            committed,
            fresh,
            f"{_COMMITTED} is stale — run `make schema` to regenerate.",
        )

    def test_top_level_shape(self):
        s = schema.build_schema()
        self.assertEqual(s["additionalProperties"], False)
        for key in ("ultimate64", "audio", "scenes", "playlist"):
            self.assertIn(key, s["properties"])
        self.assertEqual(s["properties"]["scenes"]["type"], "array")


class UnionChoicesTest(unittest.TestCase):
    """`sid_play_rate` (`str | float`) carries `choices` on only its string
    branch — a bare top-level `enum` used to apply to the whole union and
    reject the documented numeric form."""

    def setUp(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema not installed (dev dependency)")
        self.validator = Draft202012Validator(schema.build_schema())

    def test_the_numeric_form_validates(self):
        errors = list(self.validator.iter_errors({"ultimate64": {"sid_play_rate": 50.0}}))
        self.assertEqual(errors, [])

    def test_a_choice_string_validates(self):
        errors = list(self.validator.iter_errors({"ultimate64": {"sid_play_rate": "auto"}}))
        self.assertEqual(errors, [])

    def test_an_unlisted_string_is_still_rejected(self):
        errors = list(self.validator.iter_errors({"ultimate64": {"sid_play_rate": "nonsense"}}))
        self.assertTrue(errors)


class SceneFieldMetadataTest(unittest.TestCase):
    """`_scenes_schema` merges each shared field name's `choices`/`default`
    from whichever scene type `introspect.scene_types()` lists first
    (`field_docs.setdefault`) — silently correct only as long as no two
    types disagree. Nothing else pins that invariant, so this does."""

    def test_a_field_shared_by_several_scene_types_agrees_on_its_metadata(self):
        seen: dict[str, tuple[str, tuple[str, ...], object]] = {}
        for sd in introspect.scene_types():
            for fd in sd.fields:
                shape = (fd.type, fd.choices, fd.default)
                if fd.name in seen:
                    self.assertEqual(
                        seen[fd.name],
                        shape,
                        f"{fd.name!r} has diverging (type, choices, default) across scene "
                        "types — the schema's merged field docs would silently pick one.",
                    )
                else:
                    seen[fd.name] = shape


class SchemaValidatesExamplesTest(unittest.TestCase):
    def setUp(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema not installed (dev dependency)")
        self.validator = Draft202012Validator(schema.build_schema())

    def test_examples_validate(self):
        for path in paths.example_config_paths():
            with self.subTest(config=paths.example_name(path)):
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                errors = sorted(self.validator.iter_errors(data), key=lambda e: list(e.path))
                if errors:
                    msg = "\n".join(
                        f"  {'/'.join(map(str, e.path))}: {e.message}" for e in errors[:10]
                    )
                    self.fail(f"{path} failed schema validation:\n{msg}")

    def test_typo_is_rejected(self):
        # A bogus top-level key should fail (additionalProperties: false).
        bad = {"audio": {"enabledd": True}}
        self.assertTrue(list(self.validator.iter_errors(bad)))


if __name__ == "__main__":
    unittest.main()
