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
only after normalization, and concurrent writes to the same file from two
consoles (last writer wins, by design — the backup sibling is the recovery)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import MachineSettingsIsolation

from c64cast.app import config as cfgmod
from c64cast.app import config_store

# Every read and every patch measures against the machine-settings layer, so a
# real settings file on the developer's machine would change what `is_default`
# says and what a save writes. `MachineBaselineTest` supplies its own file.
_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


# `[audio].enabled` defaults on and `validate_configs` refuses it when
# sounddevice is absent, which is the CI job's environment — a fixture that
# validates only on a developer's machine tests nothing.
GOOD = (
    '[audio]\nenabled = false\n\n[color]\ndither = "atkinson"\n\n'
    '[[scenes]]\ntype = "blank"\nduration_s = 5.0\n'
)
BROKEN = '[color]\ndither = "atkinson"\n\n[[scenes\n'
# Trips `scene_factory.validate_dither_cfg`, i.e. the branch that logs a
# diagnostic and raises an exit code rather than failing to parse. Audio off
# for the same reason `GOOD` has it off — the audio check runs first, and a
# fixture that fails for a different reason on CI proves nothing.
INVALID = '[audio]\nenabled = false\n\n[color]\ndither = "nonsense"\n'
# Valid on its own, and silent about `dither` — so a machine setting for it is
# the last word, which is what makes this the fixture for the layer-blame tests.
SILENT_ON_DITHER = '[audio]\nenabled = false\n\n[[scenes]]\ntype = "blank"\nduration_s = 1.0\n'
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
        # `include_examples=False`: this fixture is about the *configured*
        # root, and coupling it to whatever ships in `c64cast/examples/` would
        # make an unrelated packaging change break tests having nothing to do
        # with examples. `ExamplesRootTest` below covers the examples root on
        # its own.
        self.store = config_store.ConfigStore([str(self.shows)], include_examples=False)


class RootsTest(StoreTestCase):
    def test_roots_are_labeled_by_their_own_basename(self):
        self.assertEqual([r.label for r in self.store.roots], ["shows"])
        self.assertEqual(self.store.roots[0].path, self.shows)

    def test_no_roots_configured_means_the_working_directory(self):
        store = config_store.ConfigStore([], cwd=self.shows, include_examples=False)
        self.assertEqual([r.path for r in store.roots], [self.shows])

    def test_two_roots_with_the_same_basename_get_distinct_labels(self):
        other = self.tmp / "b" / "shows"
        other.mkdir(parents=True)
        store = config_store.ConfigStore([str(self.shows), str(other)], include_examples=False)
        self.assertEqual([r.label for r in store.roots], ["shows", "shows-2"])

    def test_a_root_that_is_not_a_directory_is_dropped_not_fatal(self):
        with self.assertLogs("c64cast.app.config_store", level="WARNING"):
            store = config_store.ConfigStore(
                [str(self.shows), str(self.tmp / "nope")], include_examples=False
            )
        self.assertEqual([r.label for r in store.roots], ["shows"])

    def test_the_same_root_twice_is_listed_once(self):
        store = config_store.ConfigStore([str(self.shows), str(self.shows)], include_examples=False)
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
        self.assertEqual(
            self.store.index()["roots"],
            [{"label": "shows", "path": str(self.shows), "readonly": False}],
        )


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


# Two scenes that each name no media, on a host with no assets/videos to
# default to — the exact state a video scene is in the instant the console
# adds it. validate_configs (fail-fast) stops at the first; the doctor's
# collect-all pass names both.
TWO_UNRESOLVED_SCENES = (
    "[audio]\nenabled = false\n\n"
    '[[scenes]]\ntype = "video"\nduration_s = 5.0\n\n'
    '[[scenes]]\ntype = "video"\nduration_s = 5.0\n'
)


class ValidateRefTest(StoreTestCase):
    """validate_ref is the console's pre-flight: validate_text's fail-fast
    verdict on the file as it stands on disk, plus doctor.validate_load_result's
    collect-all diagnostics on top."""

    def test_a_good_config_validates_and_carries_diagnostics(self):
        # validate_ref runs with probe_environment=False (it's about this
        # config, not this machine's install), so only per-scene diagnostics
        # are expected here.
        report = self.store.validate_ref("shows/gig.toml")
        self.assertTrue(report["ok"])
        scene_diagnostics = [d for d in report["diagnostics"] if d["category"] == "scene"]
        self.assertTrue(scene_diagnostics)
        self.assertTrue(all(d["level"] == "ok" for d in scene_diagnostics))

    def test_a_file_that_will_not_parse_has_no_diagnostics(self):
        # Nothing loaded for the doctor to look at.
        (self.shows / "broken.toml").write_text(BROKEN, encoding="utf-8")
        report = self.store.validate_ref("shows/broken.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(report["diagnostics"], [])

    def test_a_config_that_fails_the_fail_fast_check_still_gets_full_diagnostics(self):
        (self.shows / "two-bad.toml").write_text(TWO_UNRESOLVED_SCENES, encoding="utf-8")
        report = self.store.validate_ref("shows/two-bad.toml")
        # The fail-fast half stops at the first bad scene...
        self.assertFalse(report["ok"])
        self.assertIn("video#0", report["error"])
        self.assertNotIn("video#1", report["error"])
        # ...but the collect-all half names every one of them.
        errors = [d for d in report["diagnostics"] if d["level"] == "error"]
        subjects = {d["subject"] for d in errors}
        self.assertIn("system/video#0", subjects)
        self.assertIn("system/video#1", subjects)


class WriteTest(StoreTestCase):
    def test_a_write_lands_and_reads_back(self):
        text = GOOD.replace("atkinson", "ordered")
        out = self.store.write("shows/gig.toml", text)
        self.assertTrue(out["ok"])
        self.assertEqual((self.shows / "gig.toml").read_text(encoding="utf-8"), text)

    def test_a_new_file_can_be_created_inside_a_root(self):
        self.store.write("shows/new.toml", GOOD)
        self.assertTrue((self.shows / "new.toml").exists())
        self.assertIn("shows/new.toml", [f["path"] for f in self.store.index()["files"]])

    def test_the_previous_contents_are_kept_as_a_hidden_sibling(self):
        out = self.store.write("shows/gig.toml", GOOD.replace("atkinson", "ordered"))
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


class PatchTest(StoreTestCase):
    """The generated form's save path: load, set, re-serialize, write.

    What these pin down is that a patch can only reach what the form rendered,
    and that the file it produces still loads — the round-trip itself
    (`load(dumps(cfg)) == cfg`) is already property-tested next door."""

    def _read(self) -> str:
        return (self.shows / "gig.toml").read_text(encoding="utf-8")

    def test_a_field_edit_lands_in_the_file(self):
        out = self.store.patch(
            "shows/gig.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        self.assertTrue(out["ok"])
        self.assertIn('dither = "ordered"', self._read())
        self.assertEqual(
            cfgmod.load_master(str(self.shows / "gig.toml")).cfgs[0].color.dither, "ordered"
        )

    def test_a_scenes_type_is_not_a_field_edit(self):
        # Changing it would reinterpret every other field in the block, and the
        # re-serialize would then drop the ones the new type has no use for —
        # a save that quietly loses what the scene said. Text editor's job.
        with self.assertRaises(config_store.EditRejected) as caught:
            self.store.patch("shows/gig.toml", [{"scene": 0, "field": "type", "value": "video"}])
        self.assertIn("as text", str(caught.exception))
        self.assertIn('type = "blank"', self._read())

    def test_reset_removes_the_key_the_way_the_form_unsets_it(self):
        # `minimal = true` is what drops it: a field back at its default is a
        # field a human wouldn't have written.
        self.store.patch("shows/gig.toml", [{"section": "color", "field": "dither", "reset": True}])
        self.assertNotIn("dither", self._read())

    def test_several_edits_apply_in_one_write(self):
        out = self.store.patch(
            "shows/gig.toml",
            [
                {"section": "color", "field": "dither", "value": "ordered"},
                {"scene": 0, "field": "duration_s", "value": 12.0},
            ],
        )
        self.assertEqual(len(out["edits"]), 2)
        text = self._read()
        self.assertIn('dither = "ordered"', text)
        self.assertIn("duration_s = 12.0", text)

    def test_the_previous_text_is_kept_as_a_sibling(self):
        out = self.store.patch(
            "shows/gig.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        self.assertEqual(out["backup"], ".gig.toml.bak")
        self.assertEqual((self.shows / ".gig.toml.bak").read_text(encoding="utf-8"), GOOD)

    def test_an_edit_that_breaks_the_config_is_refused_and_changes_nothing(self):
        with self.assertRaises(config_store.ConfigInvalid):
            self.store.patch(
                "shows/gig.toml", [{"section": "color", "field": "dither", "value": "nonsense"}]
            )
        self.assertEqual(self._read(), GOOD)

    def test_an_unknown_section_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/gig.toml", [{"section": "nope", "field": "dither", "value": "ordered"}]
            )

    def test_an_unknown_field_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch("shows/gig.toml", [{"section": "color", "field": "nope", "value": 1}])

    def test_a_field_from_another_scene_type_is_rejected(self):
        # `file` belongs to a video scene; the fixture's scene is `blank`.
        with self.assertRaises(config_store.EditRejected):
            self.store.patch("shows/gig.toml", [{"scene": 0, "field": "file", "value": "x.mp4"}])

    def test_a_scene_index_out_of_range_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch("shows/gig.toml", [{"scene": 7, "field": "duration_s", "value": 1.0}])

    def test_an_edit_naming_both_a_section_and_a_scene_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/gig.toml",
                [{"section": "color", "scene": 0, "field": "dither", "value": "ordered"}],
            )

    def test_an_edit_with_no_value_and_no_reset_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch("shows/gig.toml", [{"section": "color", "field": "dither"}])

    def test_something_that_is_not_an_edit_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch("shows/gig.toml", ["dither = ordered"])

    def test_the_dma_password_is_never_edited_or_dropped(self):
        # A round-trip would silently delete it, so the whole file is refused.
        path = self.shows / "secret.toml"
        path.write_text(GOOD + '\n[ultimate64]\ndma_password = "hunter2"\n', encoding="utf-8")
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/secret.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
            )
        self.assertIn("hunter2", path.read_text(encoding="utf-8"))

    def test_an_ensemble_master_is_not_form_editable(self):
        (self.shows / "left.toml").write_text(GOOD, encoding="utf-8")
        (self.shows / "right.toml").write_text(GOOD, encoding="utf-8")
        (self.shows / "master.toml").write_text(MASTER, encoding="utf-8")
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/master.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
            )

    def test_a_config_that_does_not_parse_has_nothing_to_edit(self):
        (self.shows / "broken.toml").write_text(BROKEN, encoding="utf-8")
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/broken.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
            )

    def test_a_patch_outside_a_root_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.patch("shows/../escaped.toml", [])

    def test_the_files_own_schema_directive_survives(self):
        (self.shows / "pinned.toml").write_text(
            "#:schema ./local.schema.json\n" + GOOD, encoding="utf-8"
        )
        self.store.patch(
            "shows/pinned.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        text = (self.shows / "pinned.toml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#:schema ./local.schema.json"))

    def test_a_file_without_a_schema_directive_does_not_grow_one(self):
        self.store.patch(
            "shows/gig.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        self.assertNotIn("#:schema", self._read())


# GOOD's scene is `blank` (color unsupported there); these need a
# color-capable scene type, so this class writes its own fixture.
GOOD_VIDEO_SCENE = (
    '[audio]\nenabled = false\n\n[color]\ndither = "atkinson"\n\n'
    '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n'
)


class SceneColorPatchTest(StoreTestCase):
    """The nested `{scene, subsection: "color", field, value}` edit form —
    the one way a save-back (or a console) reaches into one scene's
    [scenes.color] override without touching the shared [color] section."""

    def setUp(self) -> None:
        super().setUp()
        (self.shows / "gig.toml").write_text(GOOD_VIDEO_SCENE, encoding="utf-8")

    def _read(self) -> str:
        return (self.shows / "gig.toml").read_text(encoding="utf-8")

    def test_a_nested_edit_lands_in_the_scenes_color_block(self):
        self.store.patch(
            "shows/gig.toml",
            [{"scene": 0, "subsection": "color", "field": "dither", "value": "floyd-steinberg"}],
        )
        loaded = cfgmod.load_master(str(self.shows / "gig.toml")).cfgs[0]
        self.assertEqual(loaded.scenes[0].color, {"dither": "floyd-steinberg"})
        self.assertEqual(loaded.color.dither, "atkinson")  # global untouched

    def test_reset_removes_the_key_rather_than_writing_a_default(self):
        self.store.patch(
            "shows/gig.toml",
            [{"scene": 0, "subsection": "color", "field": "dither", "value": "floyd-steinberg"}],
        )
        self.store.patch(
            "shows/gig.toml",
            [{"scene": 0, "subsection": "color", "field": "dither", "reset": True}],
        )
        loaded = cfgmod.load_master(str(self.shows / "gig.toml")).cfgs[0]
        self.assertEqual(loaded.scenes[0].color, {})

    def test_an_unknown_color_field_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/gig.toml",
                [{"scene": 0, "subsection": "color", "field": "nope", "value": 1}],
            )

    def test_an_unknown_subsection_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/gig.toml",
                [{"scene": 0, "subsection": "overlays", "field": "dither", "value": "none"}],
            )

    def test_subsection_with_a_section_is_rejected(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.patch(
                "shows/gig.toml",
                [{"section": "color", "subsection": "color", "field": "dither", "value": "none"}],
            )

    def test_a_nested_edit_on_a_scene_type_that_rejects_color_fails_to_validate(self):
        (self.shows / "blank.toml").write_text(GOOD, encoding="utf-8")  # GOOD's scene is blank
        with self.assertRaises(config_store.ConfigInvalid):
            self.store.patch(
                "shows/blank.toml",
                [{"scene": 0, "subsection": "color", "field": "dither", "value": "none"}],
            )


class SceneStructureTest(StoreTestCase):
    """Adding and removing scenes — the two changes that alter the *shape* of a
    show file rather than the value of a field, and the last common edit that
    still meant opening the source.

    Every test here runs from a directory with no `assets/` in it, which is the
    state a fresh install is in and the one a developer's checkout never is. A
    blank video scene names no file, so it falls back to the default media
    directory; with the project's own populated one under the working
    directory these tests would pass on the machine they were written on and
    nowhere else."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.tmp)

    def _scenes(self) -> list[cfgmod.SceneCfg]:
        return cfgmod.load_master(str(self.shows / "gig.toml")).cfgs[0].scenes

    def test_a_blank_scene_is_appended(self):
        out = self.store.add_scene("shows/gig.toml", scene_type="video")
        self.assertTrue(out["ok"])
        self.assertEqual(out["scene"], {"added": 1, "type": "video", "copied_from": None})
        scenes = self._scenes()
        self.assertEqual([s.type for s in scenes], ["blank", "video"])

    def test_a_scene_can_be_added_before_the_media_it_will_name(self):
        """The half of this feature that only CI saw: a new video scene names
        no file yet, so the start-up pre-flight refused the write — and the
        refusal talked about `assets/videos` while the button said *add a
        scene*. The first step of building a show cannot require the show to
        already run. It saves, and the report says what is still missing."""
        out = self.store.add_scene("shows/gig.toml", scene_type="video")
        self.assertTrue(out["ok"])
        details = [w["detail"] for w in out["warnings"]]
        self.assertTrue(any("until it names its media" in d for d in details), details)
        self.assertTrue(any("assets/videos" in d for d in details), details)

    def test_the_text_editor_still_refuses_what_will_not_run(self):
        """The relaxation is the structured edits' alone. A hand-written save
        is a finished statement about the show, and the pre-flight is the only
        thing standing between it and a failure seconds into a run."""
        text = (self.shows / "gig.toml").read_text(encoding="utf-8")
        with self.assertRaises(config_store.ConfigInvalid):
            self.store.write("shows/gig.toml", text + '\n[[scenes]]\ntype = "video"\n')

    def test_a_copy_carries_the_fields_that_made_it_worth_copying(self):
        self.store.patch("shows/gig.toml", [{"scene": 0, "field": "name", "value": "opener"}])
        out = self.store.add_scene("shows/gig.toml", copy_of=0, after=0)
        self.assertEqual(out["scene"]["added"], 1)
        scenes = self._scenes()
        self.assertEqual(len(scenes), 2)
        # Verbatim, name included: inventing "opener (copy)" would be guessing
        # at what the show should call it.
        self.assertEqual([s.name for s in scenes], ["opener", "opener"])
        self.assertEqual([s.duration_s for s in scenes], [5.0, 5.0])

    def test_after_inserts_rather_than_appends(self):
        self.store.add_scene("shows/gig.toml", scene_type="video")
        self.store.add_scene("shows/gig.toml", scene_type="waveform", after=0)
        self.assertEqual([s.type for s in self._scenes()], ["blank", "waveform", "video"])

    def test_naming_both_a_type_and_a_copy_is_refused(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.add_scene("shows/gig.toml", scene_type="video", copy_of=0)
        with self.assertRaises(config_store.EditRejected):
            self.store.add_scene("shows/gig.toml")

    def test_an_unknown_type_is_refused_by_name(self):
        with self.assertRaises(config_store.EditRejected) as caught:
            self.store.add_scene("shows/gig.toml", scene_type="hologram")
        self.assertIn("hologram", str(caught.exception))
        self.assertEqual(len(self._scenes()), 1)

    def test_a_copy_of_a_scene_that_is_not_there_is_refused(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.add_scene("shows/gig.toml", copy_of=7)

    def test_a_scene_can_be_removed(self):
        self.store.add_scene("shows/gig.toml", scene_type="video")
        out = self.store.remove_scene("shows/gig.toml", 0)
        self.assertEqual(out["scene"]["removed"], 0)
        self.assertEqual([s.type for s in self._scenes()], ["video"])

    def test_the_last_scene_stays(self):
        # A playlist with nothing in it is not a show, and the refusal should
        # name the reason rather than arriving as a loader error about scenes.
        with self.assertRaises(config_store.EditRejected) as caught:
            self.store.remove_scene("shows/gig.toml", 0)
        self.assertIn("only scene", str(caught.exception))
        self.assertEqual(len(self._scenes()), 1)

    def test_a_move_reorders_and_writes(self):
        self.store.add_scene("shows/gig.toml", scene_type="video")
        self.store.add_scene("shows/gig.toml", scene_type="waveform")
        out = self.store.move_scene("shows/gig.toml", 2, 0)
        self.assertEqual(out["scene"], {"moved": 2, "to": 0, "type": "waveform", "name": None})
        self.assertEqual([s.type for s in self._scenes()], ["waveform", "blank", "video"])

    def test_a_no_op_move_is_accepted_and_idempotent(self):
        self.store.add_scene("shows/gig.toml", scene_type="video")
        out = self.store.move_scene("shows/gig.toml", 1, 1)
        self.assertTrue(out["ok"])
        self.assertEqual([s.type for s in self._scenes()], ["blank", "video"])

    def test_moving_from_an_out_of_range_index_is_refused(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.move_scene("shows/gig.toml", 5, 0)
        self.assertEqual([s.type for s in self._scenes()], ["blank"])

    def test_moving_to_an_out_of_range_index_is_refused(self):
        with self.assertRaises(config_store.EditRejected):
            self.store.move_scene("shows/gig.toml", 0, 5)
        self.assertEqual([s.type for s in self._scenes()], ["blank"])

    def test_a_structural_change_keeps_the_previous_text_like_any_other_save(self):
        self.store.add_scene("shows/gig.toml", scene_type="video")
        self.assertTrue((self.shows / ".gig.toml.bak").is_file())

    def test_an_ensemble_master_is_refused_the_way_a_patch_is(self):
        (self.shows / "master.toml").write_text(MASTER, encoding="utf-8")
        (self.shows / "left.toml").write_text(GOOD, encoding="utf-8")
        (self.shows / "right.toml").write_text(GOOD, encoding="utf-8")
        with self.assertRaises(config_store.EditRejected) as caught:
            self.store.add_scene("shows/master.toml", scene_type="video")
        self.assertIn("ensemble", str(caught.exception))


class MediaWarningTest(StoreTestCase):
    """A scene naming media that isn't there loads fine and then fails seconds
    into the run, with the link open and the C64 already reset. A warning is the
    right shape for it: the loader lets a literal path through on purpose, for
    media that arrives before showtime and for an ensemble member's own files."""

    def _check(self, text: str) -> list[dict]:
        return self.store.validate_text(text, "shows/gig.toml")["warnings"]

    def _video(self, spec: str) -> str:
        return f'[audio]\nenabled = false\n\n[[scenes]]\ntype = "video"\nfile = "{spec}"\n'

    def test_a_missing_file_is_reported_without_refusing_the_config(self):
        report = self.store.validate_text(self._video("/nope/missing.mp4"), "shows/gig.toml")
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 1)
        warning = report["warnings"][0]
        self.assertEqual(warning["scene"], 0)
        self.assertIn("missing.mp4", warning["detail"])

    def test_a_file_that_is_there_says_nothing(self):
        clip = self.tmp / "clip.mp4"
        clip.write_bytes(b"")
        self.assertEqual(self._check(self._video(str(clip))), [])

    def test_a_url_is_not_a_local_path(self):
        self.assertEqual(self._check(self._video("https://example.invalid/clip.mp4")), [])

    def test_a_glob_is_left_to_the_loader_which_already_fails_loudly(self):
        # A glob with no hits is an error, not a warning — so warning about it
        # here would be a second voice saying the same thing.
        report = self.store.validate_text(self._video("/nope/*.mp4"), "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(report["warnings"], [])

    def test_a_save_carries_the_warning_too(self):
        # The moment somebody stops looking at the check is the moment they save.
        out = self.store.write("shows/gig.toml", self._video("/nope/missing.mp4"))
        self.assertEqual(len(out["warnings"]), 1)

    def test_the_resolver_and_the_warning_agree_about_what_an_entry_is(self):
        from c64cast.app.scene_factory import missing_media

        clip = self.tmp / "clip.mp4"
        clip.write_bytes(b"")
        self.assertEqual(missing_media(f"{clip},/nope/gone.mp4"), ["/nope/gone.mp4"])
        self.assertEqual(missing_media(""), [])


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

    def test_the_form_is_json_serializable(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="blank"))
        json.dumps(config_store.describe(cfg))

    def test_every_field_carries_what_it_falls_back_to(self):
        # The form shows this before offering a `reset`, and it can't be read
        # off the introspection document — that carries the dataclass default,
        # which is a different thing on a machine with settings.
        baseline = cfgmod.Config()
        baseline.video.device = 3
        form = config_store.describe(cfgmod.Config(), baseline)
        video = next(s for s in form["sections"] if s["name"] == "video")
        device = next(f for f in video["fields"] if f["name"] == "device")
        self.assertEqual(device["baseline"], 3)

    def test_scene_fields_carry_one_too(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="blank", duration_s=9.0))
        field = next(
            f
            for f in config_store.describe(cfg)["scenes"][0]["fields"]
            if f["name"] == "duration_s"
        )
        self.assertEqual(field["baseline"], cfgmod.SceneCfg().duration_s)

    def test_is_default_is_measured_against_the_baseline(self):
        # A field the machine layer set and the file did not is *not* something
        # this file changes, so the form must not mark it as one.
        baseline = cfgmod.Config()
        baseline.video.device = 3
        cfg = cfgmod.Config()
        cfg.video.device = 3
        form = config_store.describe(cfg, baseline)
        video = next(s for s in form["sections"] if s["name"] == "video")
        device = next(f for f in video["fields"] if f["name"] == "device")
        self.assertTrue(device["is_default"])

    def test_color_is_its_own_key_not_a_flat_field(self):
        # Same treatment as `overlays`: a sparse override dict, not a plain
        # scalar field a form would render inline.
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video", color={"dither": "none"}))
        scene = config_store.describe(cfg)["scenes"][0]
        self.assertNotIn("color", {f["name"] for f in scene["fields"]})
        self.assertEqual(scene["color"], {"dither": "none"})

    def test_color_reports_empty_when_the_scene_has_no_override(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video"))
        self.assertEqual(config_store.describe(cfg)["scenes"][0]["color"], {})


class MachineBaselineTest(StoreTestCase):
    """What the machine-settings layer does to a read and to a save.

    The bug this pins: a config saved from the form used to come back carrying
    every machine setting that differed from a shipped default — so a show file
    edited on the machine with the capture card grew a `[video] device` that
    then overrode the next machine's own."""

    def setUp(self) -> None:
        super().setUp()
        settings = self.tmp / "settings.toml"
        settings.write_text("[video]\ndevice = 3\n", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"C64CAST_SETTINGS": str(settings)})
        patch.start()
        self.addCleanup(patch.stop)

    def _read(self) -> str:
        return (self.shows / "gig.toml").read_text(encoding="utf-8")

    def test_a_read_shows_the_resolved_value_but_calls_it_unchanged(self):
        form = self.store.read("shows/gig.toml")["form"]
        video = next(s for s in form["sections"] if s["name"] == "video")
        device = next(f for f in video["fields"] if f["name"] == "device")
        self.assertEqual(device["value"], 3)  # what the run will use
        self.assertTrue(device["is_default"])  # but not what this file says

    def test_a_patch_does_not_write_the_machine_setting_into_the_file(self):
        self.store.patch(
            "shows/gig.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        self.assertIn('dither = "ordered"', self._read())
        self.assertNotIn("device", self._read())

    def test_a_patch_can_still_override_the_machine_setting(self):
        self.store.patch("shows/gig.toml", [{"section": "video", "field": "device", "value": 5}])
        self.assertIn("device = 5", self._read())

    def test_reset_puts_a_field_back_to_the_machine_setting(self):
        self.store.patch("shows/gig.toml", [{"section": "video", "field": "device", "value": 5}])
        self.store.patch("shows/gig.toml", [{"section": "video", "field": "device", "reset": True}])
        self.assertNotIn("device", self._read())
        self.assertEqual(cfgmod.load(str(self.shows / "gig.toml")).video.device, 3)

    def test_a_password_in_the_machine_settings_does_not_block_editing(self):
        settings = self.tmp / "settings.toml"
        settings.write_text('[ultimate64]\ndma_password = "hunter2"\n', encoding="utf-8")
        out = self.store.patch(
            "shows/gig.toml", [{"section": "color", "field": "dither", "value": "ordered"}]
        )
        self.assertTrue(out["ok"])
        self.assertNotIn("dma_password", self._read())


class MachineLayerBlameTest(StoreTestCase):
    """A refusal has to say which file it is about.

    The trap: validation runs the whole layered load, so one stray value in
    `~/.config/c64cast/settings.toml` refuses *every* config on the host — with
    an error naming a section that is nowhere in the file on screen."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = self.tmp / "settings.toml"
        patch = mock.patch.dict(os.environ, {"C64CAST_SETTINGS": str(self.settings)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_bad_machine_setting_is_named_as_the_source(self):
        self.settings.write_text('[color]\ndither = "nonsense"\n', encoding="utf-8")
        report = self.store.validate_text(SILENT_ON_DITHER, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["layers"]), 1)
        note = report["layers"][0]
        self.assertEqual(
            (note["section"], note["key"], note["value"]), ("color", "dither", "nonsense")
        )
        self.assertEqual(note["path"], str(self.settings))

    def test_a_machine_setting_the_file_overrides_is_not_blamed(self):
        # The file says the last word on `dither`, so whatever is wrong is the
        # file's — pointing at the layer under it would send the reader away.
        self.settings.write_text('[color]\ndither = "nonsense"\n', encoding="utf-8")
        report = self.store.validate_text(INVALID, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(report["layers"], [])

    def test_a_machine_setting_the_failure_never_mentions_is_not_blamed(self):
        self.settings.write_text("[video]\ndevice = 3\n", encoding="utf-8")
        report = self.store.validate_text(INVALID, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(report["layers"], [])

    def test_nothing_is_blamed_when_the_config_loads(self):
        self.settings.write_text("[video]\ndevice = 3\n", encoding="utf-8")
        report = self.store.validate_text(GOOD, "shows/gig.toml")
        self.assertTrue(report["ok"])
        self.assertEqual(report["layers"], [])

    def test_a_settings_file_that_will_not_parse_says_so_outright(self):
        self.settings.write_text("[color\n", encoding="utf-8")
        report = self.store.validate_text(GOOD, "shows/gig.toml")
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["layers"]), 1)
        self.assertIn(str(self.settings), report["layers"][0]["error"])

    def test_a_refused_save_carries_the_attribution(self):
        self.settings.write_text('[color]\ndither = "nonsense"\n', encoding="utf-8")
        with self.assertRaises(config_store.ConfigInvalid) as caught:
            self.store.write("shows/other.toml", SILENT_ON_DITHER)
        self.assertEqual(caught.exception.report["layers"][0]["key"], "dither")


class ExamplesRootTest(unittest.TestCase):
    """The packaged examples, appended as a trailing read-only root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.shows = self.tmp / "shows"
        self.shows.mkdir()
        (self.shows / "gig.toml").write_text(GOOD, encoding="utf-8")
        self.store = config_store.ConfigStore([str(self.shows)])

    def _example_ref(self) -> str:
        return next(f["path"] for f in self.store.index()["files"] if f["root"] == "examples")

    def test_examples_root_is_listed_and_readonly(self):
        labels = {r.label: r for r in self.store.roots}
        self.assertIn("examples", labels)
        self.assertTrue(labels["examples"].readonly)
        self.assertFalse(labels["shows"].readonly)

    def test_examples_are_in_the_index_marked_readonly(self):
        files = self.store.index()["files"]
        example_files = [f for f in files if f["root"] == "examples"]
        self.assertTrue(example_files)
        self.assertTrue(all(f["readonly"] for f in example_files))
        shows_files = [f for f in files if f["root"] == "shows"]
        self.assertTrue(shows_files)
        self.assertTrue(all(not f["readonly"] for f in shows_files))

    def test_an_example_is_readable(self):
        out = self.store.read(self._example_ref())
        self.assertIsNone(out["error"])

    def test_writing_to_the_examples_root_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.write(self._example_ref(), GOOD)

    def test_patching_the_examples_root_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.patch(
                self._example_ref(), [{"section": "color", "field": "dither", "value": "ordered"}]
            )

    def test_include_examples_false_leaves_them_out(self):
        store = config_store.ConfigStore([str(self.shows)], include_examples=False)
        self.assertNotIn("examples", {r.label for r in store.roots})


class CreateTest(StoreTestCase):
    def test_a_blank_starter_is_created_and_validates(self):
        out = self.store.create("shows/new_show.toml")
        self.assertTrue(out["ok"])
        self.assertTrue((self.shows / "new_show.toml").exists())
        self.assertIsNone(self.store.read("shows/new_show.toml")["error"])

    def test_a_copy_of_an_existing_config_is_verbatim(self):
        self.store.create("shows/copy.toml", copy_of="shows/gig.toml")
        self.assertEqual(
            (self.shows / "copy.toml").read_text(encoding="utf-8"),
            (self.shows / "gig.toml").read_text(encoding="utf-8"),
        )

    def test_creating_over_an_existing_file_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.create("shows/gig.toml")

    def test_creating_in_a_directory_that_does_not_exist_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.create("shows/nowhere/new.toml")

    def test_creating_from_a_copy_source_that_does_not_exist_fails(self):
        with self.assertRaises(config_store.ConfigNotFound):
            self.store.create("shows/new.toml", copy_of="shows/nope.toml")


class CreateFromExampleTest(unittest.TestCase):
    """Duplicating a packaged example is the onboarding path — the only way
    an example, which cannot be edited in place, becomes an editable file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.shows = self.tmp / "shows"
        self.shows.mkdir()
        self.store = config_store.ConfigStore([str(self.shows)])

    def _example_ref(self) -> str:
        return next(f["path"] for f in self.store.index()["files"] if f["root"] == "examples")

    def test_duplicating_an_example_copies_it_verbatim(self):
        example_ref = self._example_ref()
        # Some packaged examples need [audio].enabled for their own feature
        # (mic capture, a soundtrack) regardless of whether this host happens
        # to have the optional `mic` extra installed — irrelevant to a verbatim
        # copy, so stand in for it rather than picking an example that avoids it.
        with mock.patch("c64cast.app.session.AUDIO_AVAILABLE", True):
            self.store.create("shows/from_example.toml", copy_of=example_ref)
        got = (self.shows / "from_example.toml").read_text(encoding="utf-8")
        want = self.store.read(example_ref)["text"]
        self.assertEqual(got, want)

    def test_creating_inside_the_examples_root_is_refused(self):
        with self.assertRaises(config_store.PathRejected):
            self.store.create("examples/mine.toml")


class DeleteTest(StoreTestCase):
    def test_delete_removes_the_file(self):
        out = self.store.delete("shows/gig.toml")
        self.assertTrue(out["ok"])
        self.assertFalse((self.shows / "gig.toml").exists())

    def test_deleting_a_missing_file_is_refused(self):
        with self.assertRaises(config_store.ConfigNotFound):
            self.store.delete("shows/nope.toml")

    def test_deleting_from_the_examples_root_is_refused(self):
        store = config_store.ConfigStore([str(self.shows)])
        example_ref = next(f["path"] for f in store.index()["files"] if f["root"] == "examples")
        with self.assertRaises(config_store.PathRejected):
            store.delete(example_ref)


if __name__ == "__main__":
    unittest.main()
