"""Tests for the documentation site renderer.

The books' prose is already guarded by tests/test_book_build.py -- it is the
same Markdown, read by the same walker. What is only true of the site is
guarded here:

  * every link the site emits resolves, whether it stays on the site or is
    sent to GitHub, and an anchor names a heading that exists;
  * the README is rendered from the file PyPI publishes, so the split rule
    that decides where the hero ends and the body begins cannot drift; and
  * the stylesheet's palette is still the PDF template's, so a reader who
    downloads the book after reading it online gets the same object.

scripts/ is not a package, so the modules are loaded by path.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import posixpath
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOCS = _REPO_ROOT / "docs"
_TEMPLATE = _DOCS / "shared" / "template.typ"
_STYLESHEET = _DOCS / "shared" / "site.css"


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which blows up if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bd = _load_script("bookdoc")
bs = _load_script("build_site")


_HREF_RE = re.compile(r'(?:href|src)="([^"]+)"')
_ID_RE = re.compile(r'\bid="([^"]+)"')

# Elements that never take a closing tag, so an unmatched one is not an error.
_VOID = frozenset(
    ["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"]
)


class _TagBalance(HTMLParser):
    """Reports any tag that closes the wrong element, or none at all."""

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in _VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        line = self.getpos()[0]
        if not self.stack:
            self.errors.append(f"{self.name}:{line} stray </{tag}>")
        elif self.stack[-1][0] != tag:
            open_tag, open_pos = self.stack.pop()
            self.errors.append(
                f"{self.name}:{line} </{tag}> closes <{open_tag}> opened at line {open_pos[0]}"
            )
        else:
            self.stack.pop()


class _BuiltSite:
    """The whole site, rendered once into a temporary directory."""

    _dir: tempfile.TemporaryDirectory[str] | None = None
    root: Path

    @classmethod
    def get(cls) -> Path:
        if cls._dir is None:
            cls._dir = tempfile.TemporaryDirectory()
            cls.root = Path(cls._dir.name) / "site"
            # build() reports what it wrote on stdout — that is for the human
            # running `make site`, not for the middle of a test run.
            with contextlib.redirect_stdout(io.StringIO()):
                bs.build(cls.root)
        return cls.root


def tearDownModule() -> None:
    if _BuiltSite._dir is not None:
        _BuiltSite._dir.cleanup()
        _BuiltSite._dir = None


class SiteBuildTest(unittest.TestCase):
    """The rendered site, end to end."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = _BuiltSite.get()
        cls.pages = {
            p.relative_to(cls.root).as_posix(): p.read_text(encoding="utf-8")
            for p in cls.root.rglob("*.html")
        }
        cls.ids = {url: set(_ID_RE.findall(text)) for url, text in cls.pages.items()}

    def test_every_published_source_became_a_page(self) -> None:
        books = bs.discover_books()
        for book in books:
            bs.load_book(book)
        expected = set(bs.build_page_map(books).values())
        self.assertEqual(expected, set(self.pages))

    def test_every_internal_link_resolves(self) -> None:
        """A link the site serves itself must name a file the site wrote.

        This is what the one-page-per-source-file layout buys: the Markdown's
        own relative links are reused with their extension changed, so a
        broken one here means the rewrite is wrong rather than the prose.
        """
        broken: list[str] = []
        for url, text in self.pages.items():
            for href in _HREF_RE.findall(text):
                if "://" in href or href.startswith("mailto:"):
                    continue
                path, _, fragment = href.partition("#")
                if not path:
                    if fragment and fragment not in self.ids[url]:
                        broken.append(f"{url} -> {href} (no such heading on this page)")
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(url), path))
                if target.endswith("/") or (self.root / target).is_dir():
                    target = posixpath.join(target.rstrip("/"), "index.html")
                if not (self.root / target).exists():
                    broken.append(f"{url} -> {href} (missing {target})")
                elif fragment and target in self.ids and fragment not in self.ids[target]:
                    broken.append(f"{url} -> {href} (no #{fragment} in {target})")
        self.assertEqual([], broken)

    def test_no_link_points_back_at_a_markdown_source(self) -> None:
        """A `.md` on the site is a rewrite that did not happen.

        Either it should have become a page of the site, or it should have
        been sent to github.com -- what it must not be is a relative path to
        a file the site never wrote.
        """
        stragglers = [
            f"{url} -> {href}"
            for url, text in self.pages.items()
            for href in _HREF_RE.findall(text)
            if href.partition("#")[0].endswith(".md") and "://" not in href
        ]
        self.assertEqual([], stragglers)

    def test_offsite_links_go_to_the_project_not_a_dead_path(self) -> None:
        """Anything not published lands on GitHub, at a path that exists."""
        repo = bs.repo_url()
        missing: list[str] = []
        for url, text in self.pages.items():
            for href in _HREF_RE.findall(text):
                for marker in (bs.GITHUB_BLOB, bs.GITHUB_TREE):
                    prefix = f"{repo}/{marker}/"
                    if not href.startswith(prefix):
                        continue
                    path = href[len(prefix) :].partition("#")[0]
                    if not (_REPO_ROOT / path).exists():
                        missing.append(f"{url} -> {href}")
        self.assertEqual([], missing)

    def test_the_assets_the_pages_point_at_were_copied(self) -> None:
        for name in ("site.css", "assets/logo.png"):
            self.assertTrue((self.root / name).is_file(), name)
        fonts = self.root / "fonts"
        self.assertTrue(sorted(fonts.glob("*.ttf")), "no fonts copied")
        # The OFL requires the license to travel with the face. A site that
        # serves the TTF without it is redistributing out of compliance.
        for face in fonts.glob("*.ttf"):
            family = face.name.split("[")[0].split("-")[0]
            self.assertTrue(
                (fonts / f"OFL-{family}.txt").is_file(),
                f"{face.name} was copied without its license",
            )

    def test_every_page_is_a_whole_document(self) -> None:
        for url, text in self.pages.items():
            with self.subTest(url=url):
                self.assertTrue(text.startswith("<!doctype html>"), url)
                self.assertIn("<h1>", text)
                self.assertIn('<meta name="viewport"', text)

    def test_every_page_is_well_formed(self) -> None:
        """Tags balance and nest on every page.

        A browser recovers from an unclosed `<li>` or a `</div>` too many by
        guessing, and the guess is usually close enough that nobody notices
        until one page of two hundred lays out wrongly. The nested-list emitter
        is where this would go wrong, and it is not visible in a diff.
        """
        problems: list[str] = []
        for url, text in self.pages.items():
            parser = _TagBalance(url)
            parser.feed(text)
            problems += parser.errors
            problems += [f"{url}: unclosed <{tag}> at line {pos[0]}" for tag, pos in parser.stack]
        self.assertEqual([], problems)

    def test_no_root_absolute_urls(self) -> None:
        """Pages serves this under /c64cast/, where a leading slash is the account."""
        absolute = [
            f"{url} -> {href}"
            for url, text in self.pages.items()
            for href in _HREF_RE.findall(text)
            if href.startswith("/")
        ]
        self.assertEqual([], absolute)

    def test_the_index_chapter_is_not_mistaken_for_a_directory(self) -> None:
        """`30-index.md` is a chapter; only `index.html` is a directory's own page."""
        text = self.pages["reference/index.html"]
        self.assertIn('href="30-index.html#a"', text)

    def test_a_books_pdf_link_is_the_published_one(self) -> None:
        for book in bs.discover_books():
            with self.subTest(book=book.slug):
                self.assertIn(
                    f"releases/latest/download/{book.meta['output']}.pdf",
                    self.pages[book.index_url],
                )


class ReadmeSplitTest(unittest.TestCase):
    """Where the landing page stops being chrome and starts being the README."""

    def setUp(self) -> None:
        self.text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_split_is_at_the_first_section(self) -> None:
        pitch, body = bs.readme_parts(self.text)
        self.assertTrue(body.startswith("## Install"), body[:40])
        self.assertIn("turns a real Commodore 64", pitch)
        # Chrome the site draws itself, and so must not render twice.
        self.assertNotIn("<img", pitch)
        self.assertNotIn("[![", pitch)
        self.assertNotIn("# c64cast", pitch)

    def test_a_readme_without_a_pitch_is_an_error(self) -> None:
        """Rather than a front page with an empty hero, which nobody notices."""
        with self.assertRaises(bd.BookError):
            bs.readme_parts("# c64cast\n\n[![CI](x)](y)\n\n## Install\n\nhi\n")
        with self.assertRaises(bd.BookError):
            bs.readme_parts("# c64cast\n\nprose\n")

    def test_the_readme_keeps_absolute_links(self) -> None:
        """It is the PyPI long description; a relative path 404s there.

        The site rewrites these at render time, which is the whole reason the
        file on disk never has to change.
        """
        relative = [
            href
            for href in re.findall(r"\]\(([^)]+)\)", self.text)
            if not href.startswith(("http://", "https://", "#"))
        ]
        self.assertEqual([], relative)


class LinkRewriteTest(unittest.TestCase):
    """The one rule that decides where every link goes."""

    def setUp(self) -> None:
        books = bs.discover_books()
        for book in books:
            bs.load_book(book)
        self.links = bs.SiteLinks(bs.build_page_map(books), bs.repo_url())
        self.repo = bs.repo_url()

    def rewrite(self, href: str, src: Path, page_url: str) -> str:
        return self.links.rewrite(href, src, page_url)

    def test_a_sibling_chapter_keeps_its_relative_form(self) -> None:
        self.assertEqual(
            "04-display-pipeline.html#fades",
            self.rewrite(
                "04-display-pipeline.md#fades",
                _DOCS / "reference" / "03-vocabulary.md",
                "reference/03-vocabulary.html",
            ),
        )

    def test_a_same_page_anchor_is_left_alone(self) -> None:
        self.assertEqual(
            "#fades",
            self.rewrite(
                "#fades", _DOCS / "reference" / "04-display-pipeline.md", "reference/x.html"
            ),
        )

    def test_a_book_readme_means_that_book(self) -> None:
        """ "see the Programmer's Reference Guide" means the book, not a contract."""
        self.assertEqual(
            "reference/",
            self.rewrite("reference/README.md", _DOCS / "caveats.md", "caveats.html"),
        )

    def test_something_the_site_does_not_publish_goes_to_github(self) -> None:
        self.assertEqual(
            f"{self.repo}/{bs.GITHUB_BLOB}/docs/architecture.md",
            self.rewrite("architecture.md", _DOCS / "caveats.md", "caveats.html"),
        )
        self.assertEqual(
            f"{self.repo}/{bs.GITHUB_TREE}/tests",
            self.rewrite("../tests/", _DOCS / "extending.md", "extending.html"),
        )

    def test_an_absolute_link_at_a_published_page_comes_back_onto_the_site(self) -> None:
        """The README's case: absolute for PyPI, relative once it is here."""
        readme = _REPO_ROOT / "README.md"
        self.assertEqual(
            "reference/03-vocabulary.html#webcam",
            self.rewrite(
                f"{self.repo}/blob/main/docs/reference/03-vocabulary.md#webcam",
                readme,
                "index.html",
            ),
        )
        self.assertEqual(
            "guide/",
            self.rewrite(f"{self.repo}/tree/main/docs/guide", readme, "index.html"),
        )

    def test_an_absolute_link_at_something_else_is_untouched(self) -> None:
        readme = _REPO_ROOT / "README.md"
        for href in (
            f"{self.repo}/blob/main/LICENSE",
            f"{self.repo}/releases/latest/download/c64cast-users-guide.pdf",
            "https://ultimate64.com/",
        ):
            with self.subTest(href=href):
                self.assertEqual(href, self.rewrite(href, readme, "index.html"))


class EmitterTest(unittest.TestCase):
    """Constructs whose HTML the walker cannot check for us."""

    def emitter(self) -> object:
        return bs.HtmlEmitter(_DOCS / "caveats.md", "caveats.html", bs.SiteLinks({}, ""))

    def convert(self, markdown: str) -> str:
        emitter = self.emitter()
        conv = bd.Converter(_DOCS / "caveats.md", 1, emitter, frozenset(), frozenset())
        conv.title = ""
        return conv.convert(markdown)

    def test_prose_is_escaped(self) -> None:
        self.assertEqual("<p>a &lt;b&gt; &amp; c</p>", self.convert("a <b> & c"))

    def test_a_nested_list_nests(self) -> None:
        out = self.convert("- a\n  - b\n- c\n")
        self.assertEqual("<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>", out)

    def test_an_ordered_list_is_an_ol(self) -> None:
        self.assertEqual("<ol><li>one</li><li>two</li></ol>", self.convert("1. one\n2. two\n"))

    def test_a_table_scrolls_inside_its_own_box(self) -> None:
        """Or the reference guide's widest tables scroll the whole page sideways."""
        out = self.convert("| a | b |\n|---|--:|\n| 1 | 2 |\n")
        self.assertIn('<div class="table-wrap">', out)
        self.assertIn('<td class="ta-right">2</td>', out)

    def test_a_callout_says_which_kind_it_is(self) -> None:
        out = self.convert("> [!WARNING]\n> careful\n")
        self.assertIn('class="callout callout-warning"', out)
        self.assertIn(">Warning</p>", out)

    def test_a_heading_carries_githubs_own_slug(self) -> None:
        """The one anchor scheme github.com, the PDF and the site all agree on."""
        out = self.convert("## Which Pixel — Dither\n")
        self.assertIn('id="which-pixel--dither"', out)
        self.assertEqual(bd.heading_slug("Which Pixel — Dither"), "which-pixel--dither")

    def test_marks_are_the_characters_the_markdown_spells_them_with(self) -> None:
        """The Typst template draws these; a browser has a whole font stack."""
        self.assertEqual("<p>✓ →</p>", self.convert("✓ →"))

    def test_a_chapter_reference_outside_a_book_is_prose(self) -> None:
        """caveats.md and the README belong to no book, so there is nothing to link."""
        self.assertEqual("<p>see Appendix A</p>", self.convert("see Appendix A"))

    def test_the_en_dash_rule_does_not_apply_here(self) -> None:
        """It is a Typst markup artifact; a browser prints `--config` as written."""
        self.assertIn("--config", self.convert("pass --config to it"))


class StylesheetTest(unittest.TestCase):
    """The site's design tokens against the PDF template's own declarations."""

    def test_the_palette_is_the_books_palette(self) -> None:
        template = _TEMPLATE.read_text(encoding="utf-8")
        css = _STYLESHEET.read_text(encoding="utf-8")
        for name in ("accent", "accent-pale", "accent-wash", "ink", "keycap-fill"):
            with self.subTest(token=name):
                m = re.search(rf"#let {re.escape(name)} = rgb\(\"(#[0-9A-Fa-f]{{6}})\"\)", template)
                self.assertIsNotNone(m, f"{name} not found in template.typ")
                assert m is not None
                # Only the light theme: the book palette was measured off a
                # printed page, so the dark values are the site's own.
                self.assertRegex(
                    css.split("@media", 1)[0],
                    rf"--{re.escape(name)}:\s*{m.group(1).lower()};",
                )

    def test_the_fonts_are_the_books_fonts(self) -> None:
        css = _STYLESHEET.read_text(encoding="utf-8")
        for face in sorted((_DOCS / "shared" / "fonts").glob("*.ttf")):
            with self.subTest(face=face.name):
                self.assertIn(f'url("fonts/{face.name}")', css)


class CheckModeTest(unittest.TestCase):
    """`--check` is what CI runs on a pull request."""

    def test_check_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, bs.build(out, write=False))
            self.assertFalse(out.exists())


class SiteSourcesTest(unittest.TestCase):
    """The documents the site publishes, as they are on disk today."""

    def test_the_standalone_documents_exist_and_have_titles(self) -> None:
        for name in bs.STANDALONE:
            with self.subTest(doc=name):
                path = _DOCS / name
                self.assertTrue(path.is_file(), path)
                self.assertTrue(
                    path.read_text(encoding="utf-8").lstrip().startswith("# "),
                    f"{name} needs a `# Title` for the site to head its page with",
                )

    def test_every_book_describes_itself_for_the_front_page(self) -> None:
        """The landing page's cards are each book's own metadata, not a copy."""
        for book in bs.discover_books():
            with self.subTest(book=book.slug):
                self.assertTrue(book.name)
                self.assertTrue(book.tagline, f"{book.slug}/book.toml has no tagline")


class ArchitectureStaysOffTest(unittest.TestCase):
    """docs/architecture* addresses a reader with the checkout already open."""

    def test_architecture_is_not_published(self) -> None:
        books = bs.discover_books()
        for book in books:
            bs.load_book(book)
        published = bs.build_page_map(books)
        self.assertNotIn(_DOCS / "architecture.md", published)
        for path in (_DOCS / "architecture").glob("*.md"):
            self.assertNotIn(path, published)


if __name__ == "__main__":
    unittest.main()
