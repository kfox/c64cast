"""Tests for the web console's config browser and, mostly, its root jail.

The escape tests are the point of this module. Everything else here — listing,
reading, validating, writing — is a convenience that the project could live
without; the jail is the thing that makes exposing part of the host's
filesystem to a browser defensible at all, so `RootJailTest` covers `..`,
absolute refs, unknown roots, non-`.toml` names, and a symlink planted *inside*
a root that points out of it (the case a purely lexical check misses).

No HTTP here: the store is deliberately app-level, so the same jail can be
tested — and reused — without a server. The route-level mapping onto status
codes lives in tests/test_web_api.py.

Not covered: a root on a case-insensitive filesystem where two labels collide
only after normalisation, and concurrent writes to the same file from two
consoles (last writer wins, by design — the backup sibling is the recovery)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c64cast.app import config as cfgmod
from c64cast.app import config_store

GOOD = '[color]\ndither = "atkinson"\n\n[[scenes]]\ntype = "blank"\nduration_s = 5.0\n'
BROKEN = '[color]\ndither = "atkinson"\n\n[[scenes\n'
# Trips `scene_factory.validate_dither_cfg`, i.e. the branch that logs a
# diagnostic and raises an exit code rather than failing to parse.
INVALID = '[color]\ndither = "nonsense"\n'
MASTER = """
[ensemble]
systems = [
    { name = "left",  config = "left.toml"  },
    { name = "right", config = "right.toml" },
]
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved because macOS hands out /var/folders/... symlinks for the
        # temp dir, and every path this module returns is real.
        self.tmp = Path(tmp.name).resolve()
        self.shows = self.tmp / "shows"
        self.shows.mkdir()
        (self.shows / "gig.toml").write_text(GOOD, encoding="utf-8")
        self.store = config_store.ConfigStore([str(self.shows)])


class RootsTest(StoreTestCase):
    def test_roots_are_labelled_by_their_own_basename(self):
        self.assertEqual([r.label for r in self.store.roots], ["shows"])
        self.assertEqual(self.store.roots[0].path, self.shows)

    def test_no_roots_configured_means_the_working_directory(self):
        store = config_store.ConfigStore([], cwd=self.shows)
        self.assertEqual([r.path for r in store.roots], [self.shows])

    def test_two_roots_with_the_same_basename_get_distinct_labels(self):
        other = self.tmp / "b" / "shows"
        other.mkdir(parents=True)
        store = config_store.ConfigStore([str(self.shows), str(other)])
        self.assertEqual([r.label for r in store.roots], ["shows", "shows-2"])

    def test_a_root_that_is_not_a_directory_is_dropped_not_fatal(self):
        with self.assertLogs("c64cast.app.config_store", level="WARNING"):
            store = config_store.ConfigStore([str(self.shows), str(self.tmp / "nope")])
        self.assertEqual([r.label for r in store.roots], ["shows"])

    def test_the_same_root_twice_is_listed_once(self):
        store = config_store.ConfigStore([str(self.shows), str(self.shows)])
        self.assertEqual(len(store.roots), 1)


class RootJailTest(StoreTestCase):
    def assertRejected(self, ref: str) -> None:
        with self.assertRaises(config_store.PathRejected, msg=f"{ref!r} was accepted"):
            self.store.resolve(ref)

    def test_dot_dot_is_rejected(self):
        self.assertRejected("shows/../../etc/passwd.toml")
        self.assertRejected("../passwd.toml")
        self.assertRejected("shows/sub/../../outside.toml")

    def test_an_absolute_path_is_not_a_ref(self):
        self.assertRejected("/etc/passwd.toml")
        self.assertRejected(str(self.tmp / "outside.toml"))

    def test_a_backslash_is_a_separator_not_a_filename(self):
        self.assertRejected("shows\\..\\..\\outside.toml")

    def test_an_unknown_root_label_is_rejected(self):
        self.assertRejected("elsewhere/gig.toml")

    def test_a_root_on_its_own_names_no_file(self):
        self.assertRejected("shows")

    def test_only_toml_is_addressable(self):
        (self.shows / "notes.txt").write_text("hi", encoding="utf-8")
        self.assertRejected("shows/notes.txt")
        self.assertRejected("shows/id_rsa")

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows")
    def test_a_symlink_out_of_a_root_is_rejected(self):
        outside = self.tmp / "secret.toml"
        outside.write_text(GOOD, encoding="utf-8")
        (self.shows / "link.toml").symlink_to(outside)
        self.assertRejected("shows/link.toml")

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows")
    def test_a_symlinked_directory_out_of_a_root_is_rejected(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "secret.toml").write_text(GOOD, encoding="utf-8")
        (self.shows / "away").symlink_to(outside, target_is_directory=True)
        self.assertRejected("shows/away/secret.toml")

    def test_a_legal_ref_resolves_inside_its_root(self):
        self.assertEqual(self.store.resolve("shows/gig.toml"), self.shows / "gig.toml")
        self.assertEqual(self.store.resolve("shows/./gig.toml"), self.shows / "gig.toml")

    def test_a_file_that_does_not_exist_yet_still_resolves(self):
        # A write to a new name is legal; only its *location* is constrained.
        self.assertEqual(self.store.resolve("shows/new.toml"), self.shows / "new.toml")

    def test_ref_for_is_the_inverse_and_says_no_outside_the_roots(self):
        self.assertEqual(self.store.ref_for(self.shows / "gig.toml"), "shows/gig.toml")
        self.assertIsNone(self.store.ref_for(self.tmp / "gig.toml"))


class IndexTest(StoreTestCase):
    def test_the_listing_finds_configs_in_subdirectories(self):
        (self.shows / "sub").mkdir()
        (self.shows / "sub" / "b.toml").write_text(GOOD, encoding="utf-8")
        refs = [f["path"] for f in self.store.index()["files"]]
        self.assertEqual(refs, ["shows/gig.toml", "shows/sub/b.toml"])

    def test_dotfiles_and_non_toml_are_not_listed(self):
        (self.shows / ".gig.toml.bak").write_text(GOOD, encoding="utf-8")
        (self.shows / "notes.txt").write_text("hi", encoding="utf-8")
        (self.shows / ".hidden").mkdir()
        (self.shows / ".hidden" / "x.toml").write_text(GOOD, encoding="utf-8")
        refs = [f["path"] for f in self.store.index()["files"]]
        self.assertEqual(refs, ["shows/gig.toml"])

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows")
    def test_a_symlink_out_of_a_root_is_not_listed_either(self):
        outside = self.tmp / "secret.toml"
        outside.write_text(GOOD, encoding="utf-8")
        (self.shows / "link.toml").symlink_to(outside)
        refs = [f["path"] for f in self.store.index()["files"]]
        self.assertEqual(refs, ["shows/gig.toml"])

    def test_the_listing_is_capped_and_says_so(self):
        for i in range(5):
            (self.shows / f"f{i}.toml").write_text(GOOD, encoding="utf-8")
        with mock.patch.object(config_store, "MAX_FILES", 3):
            index = self.store.index()
        self.assertTrue(index["truncated"])
        self.assertEqual(len(index["files"]), 3)

    def test_the_roots_come_back_with_the_listing(self):
        self.assertEqual(self.store.index()["roots"], [{"label": "shows", "path": str(self.shows)}])


class ReadTest(StoreTestCase):
    def test_a_good_config_comes_back_with_form_data(self):
        out = self.store.read("shows/gig.toml")
        self.assertEqual(out["kind"], "config")
        self.assertIsNone(out["error"])
        self.assertEqual(out["text"], GOOD)
        color = next(s for s in out["form"]["sections"] if s["name"] == "color")
        dither = next(f for f in color["fields"] if f["name"] == "dither")
        self.assertEqual(dither["value"], "atkinson")
        self.assertFalse(dither["is_default"])
        self.assertEqual([s["type"] for s in out["form"]["scenes"]], ["blank"])

    def test_a_field_left_alone_is_marked_default(self):
        out = self.store.read("shows/gig.toml")
        color = next(s for s in out["form"]["sections"] if s["name"] == "color")
        untouched = next(f for f in color["fields"] if f["name"] != "dither")
        self.assertTrue(untouched["is_default"])

    def test_the_dma_password_never_reaches_the_form(self):
        (self.shows / "secret.toml").write_text(
            '[ultimate64]\ndma_password = "hunter2"\n', encoding="utf-8"
        )
        out = self.store.read("shows/secret.toml")
        section = next(s for s in out["form"]["sections"] if s["name"] == "ultimate64")
        self.assertNotIn("dma_password", [f["name"] for f in section["fields"]])
        # The raw text is the file, though — this is an editor, not a redactor.
        self.assertIn("hunter2", out["text"])

    def test_a_broken_config_returns_its_text_and_the_parse_error(self):
        (self.shows / "bad.toml").write_text(BROKEN, encoding="utf-8")
        out = self.store.read("shows/bad.toml")
        self.assertEqual(out["text"], BROKEN)
        self.assertIsNotNone(out["error"])
        self.assertIsNone(out["form"])

    def test_an_unknown_key_is_reported_without_failing_the_read(self):
        (self.shows / "typo.toml").write_text("[video]\nfps = 30\n", encoding="utf-8")
        out = self.store.read("shows/typo.toml")
        self.assertIsNone(out["error"])
        self.assertEqual([k["key"] for k in out["unknown_keys"]], ["fps"])

    def test_an_ensemble_master_reads_but_has_no_form(self):
        for name in ("left", "right"):
            (self.shows / f"{name}.toml").write_text(GOOD, encoding="utf-8")
        (self.shows / "master.toml").write_text(MASTER, encoding="utf-8")
        out = self.store.read("shows/master.toml")
        self.assertEqual(out["kind"], "ensemble")
        self.assertEqual(out["systems"], ["left", "right"])
        self.assertIsNone(out["form"])

    def test_a_missing_file_is_not_found(self):
        with self.assertRaises(config_store.ConfigNotFound):
            self.store.read("shows/nope.toml")

    def test_an_oversized_file_is_refused_rather_than_read(self):
        (self.shows / "big.toml").write_text(GOOD + "# pad\n" * 100, encoding="utf-8")
        with mock.patch.object(config_store, "MAX_BYTES", 32):
            with self.assertRaises(config_store.ConfigTooLarge):
                self.store.read("shows/big.toml")


class ValidateTest(StoreTestCase):
    def test_a_good_config_validates(self):
        report = self.store.validate_text(GOOD, "shows/gig.toml")
        self.assertTrue(report["ok"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["systems"], ["system"])

    def test_a_parse_error_names_the_callers_file_not_the_scratch_one(self):
        report = self.store.validate_text(BROKEN, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertIn("shows/gig.toml", report["error"])
        self.assertNotIn("c64cast-check", report["error"])

    def test_a_validator_failure_carries_the_message_it_logged(self):
        report = self.store.validate_text(INVALID, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertTrue(report["error"])
        self.assertTrue(report["messages"])

    def test_the_scratch_file_is_gone_afterwards(self):
        self.store.validate_text(GOOD, "shows/gig.toml")
        leftovers = [p.name for p in self.shows.iterdir() if "c64cast-check" in p.name]
        self.assertEqual(leftovers, [])

    def test_a_master_validates_against_its_own_directory(self):
        # The per-system paths are relative to the master, so validating one
        # anywhere but beside it would report files that are not missing.
        for name in ("left", "right"):
            (self.shows / f"{name}.toml").write_text(GOOD, encoding="utf-8")
        report = self.store.validate_text(MASTER, "shows/master.toml")
        self.assertTrue(report["ok"], report["error"])
        self.assertEqual(report["systems"], ["left", "right"])


class WriteTest(StoreTestCase):
    def test_a_write_lands_and_reads_back(self):
        text = GOOD.replace("mcm", "hires")
        out = self.store.write("shows/gig.toml", text)
        self.assertTrue(out["ok"])
        self.assertEqual((self.shows / "gig.toml").read_text(encoding="utf-8"), text)

    def test_a_new_file_can_be_created_inside_a_root(self):
        self.store.write("shows/new.toml", GOOD)
        self.assertTrue((self.shows / "new.toml").exists())
        self.assertIn("shows/new.toml", [f["path"] for f in self.store.index()["files"]])

    def test_the_previous_contents_are_kept_as_a_hidden_sibling(self):
        out = self.store.write("shows/gig.toml", GOOD.replace("mcm", "hires"))
        self.assertEqual(out["backup"], ".gig.toml.bak")
        self.assertEqual((self.shows / ".gig.toml.bak").read_text(encoding="utf-8"), GOOD)

    def test_a_config_that_does_not_load_is_refused_and_changes_nothing(self):
        with self.assertRaises(config_store.ConfigInvalid) as caught:
            self.store.write("shows/gig.toml", BROKEN)
        self.assertIn("shows/gig.toml", caught.exception.report["error"])
        self.assertEqual((self.shows / "gig.toml").read_text(encoding="utf-8"), GOOD)
        self.assertFalse((self.shows / ".gig.toml.bak").exists())

    def test_a_write_outside_a_root_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.write("shows/../escaped.toml", GOOD)
        self.assertFalse((self.tmp / "escaped.toml").exists())

    def test_a_write_into_a_directory_that_does_not_exist_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.write("shows/nowhere/new.toml", GOOD)

    def test_an_oversized_write_is_refused(self):
        with mock.patch.object(config_store, "MAX_BYTES", 8):
            with self.assertRaises(config_store.ConfigTooLarge):
                self.store.write("shows/gig.toml", GOOD)


class DescribeTest(unittest.TestCase):
    def test_every_section_of_a_blank_config_is_all_defaults(self):
        form = config_store.describe(cfgmod.Config())
        self.assertTrue(form["sections"])
        changed = [
            (s["name"], f["name"])
            for s in form["sections"]
            for f in s["fields"]
            if not f["is_default"]
        ]
        self.assertEqual(changed, [])

    def test_scene_fields_are_filtered_to_the_scene_type(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="wled"))
        names = {f["name"] for f in config_store.describe(cfg)["scenes"][0]["fields"]}
        # `file` is a video-scene field; `sink_width` is this type's own.
        self.assertIn("sink_width", names)
        self.assertNotIn("file", names)

    def test_the_form_is_json_serialisable(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="blank"))
        json.dumps(config_store.describe(cfg))


if __name__ == "__main__":
    unittest.main()
