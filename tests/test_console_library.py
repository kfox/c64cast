"""Tests for the web console's favorites + recents store.

`_load`'s tolerant contract — "a missing, corrupt, or wrong-shaped file reads
as an empty library rather than raising" — is the part worth testing hardest,
since nothing in tests/test_web_api.py's HTTP-level library tests can write a
corrupt or wrong-shaped console.json (they only ever drive a freshly
constructed store). Also covered: MAX_RECENTS truncation, record_recent's
move-to-front dedup, MAX_FAVORITES, and the ref-length ceiling both methods
share.

Not covered: concurrent writers racing on the same file — `self._lock` only
serializes calls within one process, and two consoles pointed at the same
host is the same last-writer-wins trade `ConfigStore` already accepts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c64cast.app import console_library


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "console.json"
        self.library = console_library.ConsoleLibrary(path=self.path)


class ToleranceTest(LibraryTestCase):
    def test_a_missing_file_reads_as_an_empty_library(self):
        self.assertEqual(self.library.as_dict(), {"favorites": [], "recents": []})

    def test_a_corrupt_file_reads_as_an_empty_library(self):
        self.path.write_text("not json{", encoding="utf-8")
        self.assertEqual(self.library.as_dict(), {"favorites": [], "recents": []})

    def test_a_non_dict_top_level_reads_as_an_empty_library(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self.library.as_dict(), {"favorites": [], "recents": []})

    def test_a_null_favorites_value_reads_as_empty_rather_than_raising(self):
        # `dict.get(key, default)`'s default only applies when `key` is
        # absent, so `{"favorites": null}` used to reach the list
        # comprehension unguarded and raise TypeError straight out of
        # as_dict — this is the regression test for that.
        self.path.write_text('{"favorites": null, "recents": []}', encoding="utf-8")
        self.assertEqual(self.library.as_dict(), {"favorites": [], "recents": []})

    def test_a_non_list_recents_value_reads_as_empty_rather_than_raising(self):
        self.path.write_text('{"favorites": [], "recents": 5}', encoding="utf-8")
        self.assertEqual(self.library.as_dict(), {"favorites": [], "recents": []})

    def test_a_string_favorites_value_does_not_load_as_its_own_characters(self):
        # A `str` is iterable, so an unguarded `for f in raw.get(...)` would
        # accept "hello" as five one-character favorites instead of refusing
        # the wrong-shaped value.
        self.path.write_text('{"favorites": "hello", "recents": []}', encoding="utf-8")
        self.assertEqual(self.library.as_dict()["favorites"], [])

    def test_non_string_favorites_are_dropped(self):
        self.path.write_text('{"favorites": ["a", 5, null, "b"], "recents": []}', encoding="utf-8")
        self.assertEqual(self.library.as_dict()["favorites"], ["a", "b"])

    def test_recents_missing_a_ref_are_dropped(self):
        self.path.write_text(
            '{"favorites": [], "recents": [{"at": 1}, {"ref": "x", "at": 2}]}', encoding="utf-8"
        )
        self.assertEqual([r["ref"] for r in self.library.as_dict()["recents"]], ["x"])

    def test_recents_with_a_non_numeric_at_are_dropped(self):
        self.path.write_text(
            '{"favorites": [], "recents": [{"ref": "x", "at": "later"}]}', encoding="utf-8"
        )
        self.assertEqual(self.library.as_dict()["recents"], [])


class FavoritesTest(LibraryTestCase):
    def test_setting_a_favorite_persists_across_instances(self):
        self.library.set_favorite("shows/gig.toml", True)
        reloaded = console_library.ConsoleLibrary(path=self.path)
        self.assertEqual(reloaded.as_dict()["favorites"], ["shows/gig.toml"])

    def test_unsetting_a_favorite_removes_it(self):
        self.library.set_favorite("shows/gig.toml", True)
        favorites = self.library.set_favorite("shows/gig.toml", False)
        self.assertEqual(favorites, [])

    def test_an_empty_ref_is_a_no_op(self):
        favorites = self.library.set_favorite("", True)
        self.assertEqual(favorites, [])
        self.assertEqual(self.library.as_dict()["favorites"], [])

    def test_a_ref_over_the_byte_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            self.library.set_favorite("x" * (console_library._MAX_REF_BYTES + 1), True)

    def test_favorites_are_capped(self):
        with mock.patch.object(console_library, "MAX_FAVORITES", 2):
            self.library.set_favorite("a", True)
            self.library.set_favorite("b", True)
            with self.assertRaises(ValueError):
                self.library.set_favorite("c", True)
        self.assertEqual(self.library.as_dict()["favorites"], ["a", "b"])

    def test_unfavoriting_is_never_blocked_by_the_cap(self):
        with mock.patch.object(console_library, "MAX_FAVORITES", 1):
            self.library.set_favorite("a", True)
            favorites = self.library.set_favorite("a", False)
        self.assertEqual(favorites, [])


class RecentsTest(LibraryTestCase):
    def test_recording_a_recent_persists_across_instances(self):
        self.library.record_recent("shows/gig.toml")
        reloaded = console_library.ConsoleLibrary(path=self.path)
        self.assertEqual([r["ref"] for r in reloaded.as_dict()["recents"]], ["shows/gig.toml"])

    def test_recording_an_existing_ref_moves_it_to_the_front_without_duplicating(self):
        self.library.record_recent("a")
        self.library.record_recent("b")
        recents = self.library.record_recent("a")
        self.assertEqual([r["ref"] for r in recents], ["a", "b"])

    def test_recents_are_capped_and_the_newest_survive(self):
        for i in range(console_library.MAX_RECENTS + 1):
            recents = self.library.record_recent(f"clip{i}")
        self.assertEqual(len(recents), console_library.MAX_RECENTS)
        self.assertEqual(recents[0]["ref"], f"clip{console_library.MAX_RECENTS}")
        self.assertNotIn("clip0", [r["ref"] for r in recents])

    def test_an_empty_ref_is_a_no_op(self):
        recents = self.library.record_recent("")
        self.assertEqual(recents, [])
        self.assertEqual(self.library.as_dict()["recents"], [])

    def test_a_ref_over_the_byte_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            self.library.record_recent("x" * (console_library._MAX_REF_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
