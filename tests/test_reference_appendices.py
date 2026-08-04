"""Tests for the generated reference appendices + the committed files.

Guards the same thing tests/test_schema.py guards for the JSON schema, for the
same reason: the files are generated but committed, so nothing forces them to
be regenerated except a test that notices.

  * every file scripts/gen_reference_appendices.py owns matches a fresh run
    (so `make reference-appendices` was run after a config/CLI/registry
    change), and
  * the text it emits is safe for both renderers -- it survives
    scripts/build_book.py's deliberately small Markdown subset, which rejects
    what it cannot translate rather than dropping it.

The second is the one that catches surprises. Help strings are written for a
terminal, where nothing is markup, so a new `--flag` or a `str | None` type
lands in a table cell as an en dash or an extra column. The book build would
fail on it eventually; failing here says which string and why.

scripts/ is not a package, so both modules are loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

from c64cast import introspect

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which blows up if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("gen_reference_appendices")
bb = _load("build_book")


class FreshnessTest(unittest.TestCase):
    def test_committed_files_are_fresh(self):
        stale = []
        for path, build in gen.APPENDICES.items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != gen.render(build):
                stale.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(
            stale,
            [],
            "generated files are stale — run `make reference-appendices`",
        )

    def test_every_generated_file_says_so(self):
        # The marker is how the generator finds the files it owns, and how a
        # human who opened one in an editor is warned before typing into it.
        for path in gen.APPENDICES:
            with self.subTest(file=path.name):
                fields, _, _ = bb.parse_front_matter(path.read_text(encoding="utf-8"), path)
                self.assertEqual(fields.get("generated"), "true")

    def test_the_appendices_cover_the_reference_book(self):
        # A-I and the index are generated; the introduction, the seven chapters
        # and the glossary are not. If a hand-written chapter ever acquires the
        # marker, the next `make reference-appendices` would not touch it and
        # the drift guard above would silently pass on a file nobody generates.
        generated = {p for p in gen.APPENDICES if p.parent == gen.REFERENCE_DIR}
        self.assertEqual(len(generated), 10)
        for path in bb.discover_chapters(gen.REFERENCE_DIR):
            fields, _, _ = bb.parse_front_matter(path.read_text(encoding="utf-8"), path)
            with self.subTest(chapter=path.name):
                self.assertEqual(fields.get("generated") == "true", path in generated)


class ConverterSafetyTest(unittest.TestCase):
    """The generated Markdown has to survive the book converter.

    Every failure mode here is a real one that help text has already produced:
    an unbackticked `--flag` (Typst renders `--` as an en dash, so the
    converter refuses it), and a bare `|` in a type or a quoted choice list
    (which splits the table row and changes the cell count).
    """

    def test_every_generated_file_converts(self):
        for path in gen.APPENDICES:
            with self.subTest(file=path.name):
                paths = bb.discover_chapters(path.parent)
                anchors = bb.section_anchors(paths)
                # The anchors are not optional here: the index is nothing but
                # links at sections, and the converter refuses one it cannot
                # resolve rather than emitting a dead destination.
                chapter = bb.load_chapter(path, bb.chapter_numbers(paths), anchors)
                self.assertTrue(chapter.title)

    def test_the_reference_book_builds(self):
        self.assertIn("#show: guide.with(", bb.build(gen.REFERENCE_DIR))


class IndexTest(unittest.TestCase):
    """The generated index.

    Freshness is the drift guard's job. What is particular to the index is that
    it is made almost entirely of links into the rest of the book, so a section
    renamed without a regeneration turns every locator into it stale -- and a
    stale locator is the one failure a reader meets rather than the build.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = gen.INDEX_PATH.read_text(encoding="utf-8")
        cls.rows = dict(re.findall(r"^\| \*\*(.+?)\*\* \| (.+?) \|$", cls.text, re.M))

    def test_every_locator_names_a_section_that_exists(self):
        anchors = bb.section_anchors(bb.discover_chapters(gen.REFERENCE_DIR))
        links = re.findall(r"\]\((\d+-[\w.-]+\.md)#([\w-]+)\)", self.text)
        self.assertGreater(len(links), 500, "the index lost most of its locators")
        for filename, slug in links:
            with self.subTest(link=f"{filename}#{slug}"):
                self.assertIn(bb.section_label(Path(filename).stem, slug), anchors)

    def test_it_is_not_an_appendix(self):
        # No `number`, which is what makes it render after Appendix J as a
        # plain heading instead of claiming a letter of its own.
        fields, _, _ = bb.parse_front_matter(self.text, gen.INDEX_PATH)
        self.assertNotIn("number", fields)

    def test_it_does_not_index_itself(self):
        self.assertNotIn(gen.INDEX_PATH.name, self.text)

    def test_the_section_written_about_a_term_leads_its_entry(self):
        # Appendix A has a row for every configuration field, so ordering by
        # position alone would answer "where is dither explained" with the
        # table rather than with the section that explains it.
        self.assertTrue(
            self.rows["`dither`"].startswith("[Which Pixel Takes Which"),
            self.rows["`dither`"],
        )

    def test_a_section_title_is_an_entry_in_its_own_words(self):
        # The concept entries are what let a reader look up the thing before
        # they know what it is called.
        for title in ("Companding", "The Audio Slot", "Fades"):
            with self.subTest(title=title):
                self.assertIn(title, self.rows)


class IndexTermTest(unittest.TestCase):
    """The pure parts of the index: what counts as a name, and where it files."""

    def test_a_span_names_every_key_it_contains(self):
        self.assertEqual(
            gen.mentions("set `[color].dither` to `ordered`"),
            {"[color].dither", "color.dither", "dither", "ordered"},
        )

    def test_a_bracketed_section_does_not_credit_the_bare_word(self):
        # `audio` is a scene key. Left in, it would collect every mention of
        # the `[audio]` section as though the two were the same setting.
        self.assertNotIn("audio", gen.mentions("`[audio].backend`"))

    def test_a_flag_is_found_inside_a_command(self):
        self.assertIn("--save-settings", gen.mentions("run `c64cast --save-settings`"))

    def test_a_title_that_is_only_a_name_is_not_a_concept(self):
        for title in ("`sid_panning`", "`[hardware]`"):
            with self.subTest(title=title):
                self.assertEqual(gen.concept(title), "")

    def test_a_title_qualified_by_a_name_files_under_the_concept(self):
        self.assertEqual(gen.concept("Companding — `dac_curve`"), "Companding")
        # Only a *trailing* name is a qualifier; an em dash joining two phrases
        # is part of the title.
        self.assertEqual(
            gen.concept("Broadcast — Fixtures React to the Music"),
            "Broadcast — Fixtures React to the Music",
        )

    def test_an_entry_files_under_the_word_it_is_looked_up_by(self):
        self.assertEqual(gen.sort_key("--config"), "config")
        self.assertEqual(gen.sort_key("[audio]"), "audio")
        self.assertEqual(gen.sort_key("The Audio Slot"), "audio slot")

    def test_a_value_or_an_abbreviation_is_never_a_term(self):
        keys = gen.code_terms()
        self.assertFalse({k.lower() for k in keys} & gen._INDEX_STOP_WORDS)
        self.assertFalse([k for k in keys if len(k) < gen._MIN_TERM_LEN])

    def test_a_field_is_qualified_only_where_two_sections_share_the_name(self):
        keys = gen.code_terms()
        # `dither` is [color]'s dithering and [audio]'s noise shaping.
        self.assertIn("color.dither", keys)
        self.assertIn("audio.dither", keys)
        # `agc` belongs to [dsp] alone, so a `dsp.agc` row would be a second
        # entry pointing where the `agc` one already points.
        self.assertIn("agc", keys)
        self.assertNotIn("dsp.agc", keys)


class LiveMarkTest(unittest.TestCase):
    """The two *live* marks Appendices A and B carry.

    The alias map is hand-written, so both of its ends can rot independently:
    a renamed config field leaves the mark on nothing, and a retired live
    target leaves it pointing at a row Appendix F no longer has.
    """

    def test_every_alias_names_a_real_config_field(self):
        by_section = {s.name: {fd.name for fd in s.fields} for s in introspect.config_sections()}
        by_section["scenes"] = {fd.name for fd in introspect._scene_field_docs()}
        for section, field in gen._LIVE_TUNABLE:
            with self.subTest(field=f"{section}.{field}"):
                self.assertIn(section, by_section)
                self.assertIn(field, by_section[section])

    def test_every_alias_names_a_real_live_target(self):
        targets = {t.target for t in introspect.live_targets()}
        for (section, field), target in gen._LIVE_TUNABLE.items():
            with self.subTest(field=f"{section}.{field}"):
                self.assertIn(target, targets)

    def test_the_marks_reach_the_committed_appendices(self):
        # [color].dither is the one that does not join by name -- it is
        # mode.dither_method -- so it is the one worth asserting lands.
        text = (gen.REFERENCE_DIR / "20-appendix-a-configuration.md").read_text(encoding="utf-8")
        self.assertIn("`mode.dither_method`", text)
        # palette_mode carries both marks, which is why they are worded apart.
        scenes = (gen.REFERENCE_DIR / "21-appendix-b-scene-types.md").read_text(encoding="utf-8")
        self.assertIn("*Live-tunable*", scenes)
        self.assertIn("*Menu-live*", scenes)

    def test_a_bare_name_match_would_have_marked_the_wrong_dither(self):
        # [audio].dither is the 4-bit DAC's noise shaping and has nothing to do
        # with the display pipeline; it must stay unmarked.
        audio = next(s for s in introspect.config_sections() if s.name == "audio")
        fd = next(f for f in audio.fields if f.name == "dither")
        self.assertEqual(gen.marks("audio", fd), "")


class DeclaredByTest(unittest.TestCase):
    """The card's compression of Appendix F's owner list."""

    def _target(self, owners, holder="source"):
        return introspect.LiveTargetDoc(
            target=f"{holder}.x",
            holder=holder,
            group="Generator",
            kind="scalar",
            owners=tuple(owners),
        )

    def test_a_sole_owner_is_named(self):
        self.assertEqual(gen.declared_by(self._target(["moire2"]), {"source": 20}), "`moire2`")

    def test_several_owners_are_counted_in_the_groups_noun(self):
        owners = [f"g{i}" for i in range(14)]
        self.assertEqual(gen.declared_by(self._target(owners), {"source": 20}), "14 generators")

    def test_every_owner_is_all(self):
        owners = [f"g{i}" for i in range(20)]
        self.assertEqual(gen.declared_by(self._target(owners), {"source": 20}), "all")

    def test_every_holder_has_a_noun(self):
        # A new live-tune holder with no noun would raise mid-generation.
        for holder in gen._holder_totals():
            self.assertIn(holder, gen._OWNER_NOUNS)


class LiveParamSpellingTest(unittest.TestCase):
    """Appendix E writes a knob the way a `cc_map` has to spell it."""

    def test_a_generator_param_carries_its_holder(self):
        targets = {t.target for t in introspect.live_targets()}
        text = (gen.REFERENCE_DIR / "24-appendix-e-generators-effects.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("`source.speed`", text)
        self.assertIn("`effect.decay`", text)
        # Whatever Appendix E prints has to be a row Appendix F has.
        for name in re.findall(r"`((?:source|effect)\.[a-z_]+)`", text):
            with self.subTest(target=name):
                self.assertIn(name, targets)


class TextEscapingTest(unittest.TestCase):
    """The helpers that make terminal help text safe as Markdown."""

    def test_a_bare_flag_is_backticked(self):
        self.assertEqual(gen.prose("mute with --no-audio"), "mute with `--no-audio`")

    def test_a_long_flag_paired_with_a_short_one_is_still_found(self):
        # Help text writes the pair as `-u/--url`; an earlier lookbehind
        # excluded `/` and left the long form bare, which the converter
        # rejected as an en dash.
        self.assertEqual(gen.prose("saves -u/--url"), "saves `-u/--url`")

    def test_a_flag_already_in_a_code_span_is_left_alone(self):
        # Some help strings mark up their own; double-wrapping would put
        # literal backticks in the rendered output.
        self.assertEqual(gen.prose("see `--describe`"), "see `--describe`")

    def test_table_cells_escape_pipes(self):
        rows = gen.table(["A", "B"], [["`str | None`", "'cc'|'note'"]])
        self.assertIn(r"`str \| None`", rows[2])
        self.assertIn(r"'cc'\|'note'", rows[2])

    def test_an_escaped_pipe_survives_the_round_trip(self):
        # Escaped on the way out, unescaped by the converter before inline
        # parsing -- so a code span gets a pipe, not a backslash it would print.
        markdown = "\n".join(gen.table(["Field", "Type"], [["`x`", "`str | None`"]]))
        conv = bb.Converter(_REPO_ROOT / "docs" / "reference" / "99-test.md", 1)
        conv.title = "Test"
        typst = conv.convert(markdown)
        self.assertIn("columns: 2", typst)
        self.assertIn("str | None", typst)
        self.assertNotIn("\\|", typst)

    def test_an_empty_table_is_omitted_entirely(self):
        # The converter needs the alignment row to see a table at all, so a
        # header with no body would render as a lone empty box.
        self.assertEqual(gen.table(["A"], []), [])

    def test_an_enormous_default_is_summarised(self):
        # [midi_control].cc_map's default is two dozen mappings and 2,500
        # characters; printed in full it pushes the column off the page.
        self.assertEqual(gen.fmt_default(list(range(500))), "*500 shipped entries*")

    def test_an_ordinary_default_is_printed_verbatim(self):
        self.assertEqual(gen.fmt_default("http://192.168.2.64"), "`'http://192.168.2.64'`")

    def test_first_sentence_survives_an_abbreviation(self):
        text = "Blurs the frame, e.g. to soften dither. Not reactive."
        self.assertEqual(gen.first_sentence(text), "Blurs the frame, e.g. to soften dither.")


if __name__ == "__main__":
    unittest.main()
