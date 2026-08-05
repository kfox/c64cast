#!/usr/bin/env python3
"""Render the books and the user docs into the static site at docs/_site/.

The web counterpart of `scripts/build_book.py`: same Markdown, same dialect,
same anchors, a different emitter. `scripts/bookdoc.py` owns the reading and
every check; this module only says what each construct looks like as HTML, and
`docs/shared/site.css` owns the design.

    python scripts/build_site.py                # write docs/_site/
    python scripts/build_site.py --check        # parse only

`make site` runs this; `.github/workflows/pages.yml` publishes what it writes.

The site is one page per source file, mirroring the repo with `.html` for `.md`:

    /                       README, under a generated hero
    /guide/                 that book's contents page
    /guide/04-setting-up.html
    /caveats.html /troubleshooting.html /extending.html

which is what makes link rewriting nearly free -- `[Fades](04-x.md#fades)` is
already relative and already anchored on GitHub's slug rule, so it needs only
its extension changed. A link at something the site does not publish
(`docs/architecture.md`, `../c64cast/audio.py`, `LICENSE`) is sent to GitHub
instead, so nothing 404s and nothing has to be restated.

Every URL the site emits is relative. GitHub Pages serves this under
`/c64cast/`, and a root-absolute `/site.css` would resolve to the user's
account page rather than the project's -- the same file also has to work from
`python -m http.server -d docs/_site`.

Stdlib only, and no import of `c64cast`, for the reason `build_book.py` says.
"""

from __future__ import annotations

import argparse
import html
import posixpath
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# scripts/ is not a package and this module is loaded by path as often as it is
# run; see the same note in build_book.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bookdoc import (  # noqa: E402
    REPO_ROOT,
    BookError,
    Chapter,
    Converter,
    Emitter,
    ListItem,
    SectionRef,
    book_version,
    chapter_numbers,
    discover_chapters,
    file_section_slugs,
    load_book_toml,
    load_chapter,
    parse_front_matter,
    section_anchors,
    section_label,
)

DOCS = REPO_ROOT / "docs"
DEFAULT_OUT = DOCS / "_site"

# Documents outside any book that the site publishes. Everything else under
# docs/ -- architecture.md and its topic directory, each book's authoring
# README -- addresses somebody editing the code with the checkout already open,
# and is left on github.com where that reader is.
STANDALONE = ("caveats.md", "troubleshooting.md", "extending.md")

# Where a link the site cannot serve is sent instead. `main` and not the
# release tag: the site is built from `main`, so a line of prose and the source
# it points at are the same age.
GITHUB_BLOB = "blob/main"
GITHUB_TREE = "tree/main"

# Each release attaches a version-stamped PDF of every book, and `latest`
# redirects to the newest. Deliberately not a pinned tag: the site tracks
# `main` and is ahead of the PDFs, so the honest offer is "the current one".
RELEASE_PDF = "releases/latest/download/{output}.pdf"

# Copied verbatim into the site. The fonts' licenses travel with them because
# the OFL requires it, not as a courtesy; see docs/shared/fonts/README.md.
FONT_DIR = DOCS / "shared" / "fonts"
STYLESHEET = DOCS / "shared" / "site.css"
LOGO = REPO_ROOT / "assets" / "logo.png"

_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(markup: str) -> str:
    """Converted inline markup as bare text, for an `alt` or a `<title>`."""
    return html.unescape(_TAG_RE.sub("", markup))


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def attr(text: str) -> str:
    return html.escape(text, quote=True)


def repo_url() -> str:
    """The project's GitHub URL, read from the metadata that publishes it.

    Not spelled here as a constant: `pyproject.toml` already names it for PyPI,
    and a fork that changes it should not have to find a second copy.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    url = data.get("project", {}).get("urls", {}).get("Source")
    if not isinstance(url, str) or not url:
        raise BookError("pyproject.toml has no [project.urls] Source")
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# The page map
# ---------------------------------------------------------------------------


@dataclass
class Book:
    """One book directory, with everything both its pages and the site need."""

    dir: Path
    slug: str  # "guide"
    meta: dict[str, str]  # its [book] table
    chapters: list[Chapter] = field(default_factory=list)
    # The two whole-book facts every chapter of it is converted against: which
    # chapter numbers exist, and every section label defined anywhere in it.
    numbers: frozenset[str] = frozenset()
    anchors: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        """What the book is called in running text: "User's Guide"."""
        # The bound layouts carry the series volume; the card has only its
        # subtitle, which is its name.
        return self.meta.get("volume") or self.meta["subtitle"]

    @property
    def tagline(self) -> str:
        return self.meta.get("tagline", "")

    @property
    def index_url(self) -> str:
        return f"{self.slug}/index.html"

    @property
    def pdf(self) -> str:
        return RELEASE_PDF.format(output=self.meta["output"])

    def page_url(self, path: Path) -> str:
        return f"{self.slug}/{path.stem}.html"

    @property
    def chapter_urls(self) -> dict[str, str]:
        """Which page each `Chapter 4` / `Appendix F` reference lands on."""
        return {c.number: self.page_url(c.path) for c in self.chapters if c.number is not None}


def discover_books() -> list[Book]:
    """Every book under docs/, in the order the reader meets them.

    Found by looking for `book.toml` rather than listed here, so adding a
    fourth book is a directory and not an edit to this file. The order is the
    series order: the guide is read first, the reference is opened at a page,
    the card is printed.
    """
    order = {"guide": 0, "reference": 1, "card": 2}
    dirs = sorted(
        (p.parent for p in DOCS.glob("*/book.toml")),
        key=lambda d: (order.get(d.name, len(order)), d.name),
    )
    if not dirs:
        raise BookError(f"no books (docs/*/book.toml) found under {DOCS}")
    return [Book(dir=d, slug=d.name, meta=load_book_toml(d)) for d in dirs]


def build_page_map(books: list[Book]) -> dict[Path, str]:
    """Every source file the site publishes, mapped to its URL.

    This is what decides whether a link stays on the site or is sent to
    GitHub, so it is built once and consulted rather than re-derived per link.
    A book's `README.md` maps to its contents page: prose that says "see the
    Programmer's Reference Guide" means the book, and on the site the contents
    page *is* the book.
    """
    pages: dict[Path, str] = {REPO_ROOT / "README.md": "index.html"}
    for book in books:
        pages[book.dir / "README.md"] = book.index_url
        for path in discover_chapters(book.dir):
            pages[path] = book.page_url(path)
    for name in STANDALONE:
        pages[DOCS / name] = f"{Path(name).stem}.html"
    return pages


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


class SiteLinks:
    """Rewrites one page's links: to the site where it can, to GitHub where not."""

    def __init__(self, pages: dict[Path, str], repo: str) -> None:
        self.pages = pages
        self.repo = repo

    def rewrite(self, href: str, src: Path, page_url: str) -> str:
        if href.startswith("#") or "://" in href or href.startswith("mailto:"):
            return self._absolute(href, page_url)

        path, _, fragment = href.partition("#")
        target = (src.parent / path).resolve()
        url = self.pages.get(target)
        if url is not None:
            return self.relative(url, page_url) + (f"#{fragment}" if fragment else "")
        return self.github(target) + (f"#{fragment}" if fragment else "")

    def _absolute(self, href: str, page_url: str) -> str:
        """An already-absolute link, pulled back onto the site if it names a page.

        This is the README's case and only the README's: it is the PyPI long
        description, so every in-repo link in it has to be an absolute
        github.com URL. A reader who is already on the site should not be sent
        back to the code host to read the next chapter.
        """
        prefix = f"{self.repo}/"
        if not href.startswith(prefix):
            return href
        rest = href[len(prefix) :]
        for marker in (GITHUB_BLOB, GITHUB_TREE):
            if not rest.startswith(marker + "/"):
                continue
            path, _, fragment = rest[len(marker) + 1 :].partition("#")
            # A `tree/` link names a directory, which on the site is whatever
            # page that directory reads as -- a book's contents page.
            candidates = [REPO_ROOT / path, REPO_ROOT / path / "README.md"]
            for candidate in candidates:
                url = self.pages.get(candidate)
                if url is not None:
                    return self.relative(url, page_url) + (f"#{fragment}" if fragment else "")
        return href

    def github(self, target: Path) -> str:
        try:
            path = target.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            raise BookError(f"{target} is outside the repository") from None
        marker = GITHUB_TREE if target.is_dir() else GITHUB_BLOB
        return f"{self.repo}/{marker}/{path}"

    @staticmethod
    def relative(url: str, page_url: str) -> str:
        """`url` as seen from `page_url`, both site-relative.

        Relative and never root-absolute: Pages serves the site from
        `/c64cast/`, where a leading slash means the account, not the project.
        """
        rel = posixpath.relpath(url, posixpath.dirname(page_url))
        # A directory's own index reads better as `guide/` than as
        # `guide/index.html`, and is the URL a reader would type. Matched on
        # the basename and not the tail of the string: the reference guide has
        # a chapter called `30-index.md`, which is not anybody's directory.
        if posixpath.basename(rel) != "index.html":
            return rel
        return rel[: -len("index.html")] or "./"


# ---------------------------------------------------------------------------
# The emitter
# ---------------------------------------------------------------------------

_CALLOUT_TITLES = {
    "NOTE": "Note",
    "TIP": "Tip",
    "WARNING": "Warning",
    "IMPORTANT": "Important",
    "CAUTION": "Caution",
}


class HtmlEmitter(Emitter):
    """Every construct as HTML. One per page: it resolves that page's links.

    It also collects what the chrome around the page needs -- the headings for
    the "on this page" list, the figures to copy -- rather than have a second
    pass re-read the Markdown to find them.
    """

    def __init__(
        self,
        src: Path,
        page_url: str,
        links: SiteLinks,
        chapter_urls: dict[str, str] | None = None,
    ) -> None:
        self.src = src
        self.page_url = page_url
        self.links = links
        # Which page each `Chapter 4` / `Appendix F` reference lands on. Empty
        # for a page that belongs to no book, where the walker leaves such a
        # reference as prose.
        self.chapter_urls = chapter_urls or {}
        self.headings: list[tuple[int, str, str]] = []  # (level, slug, markup)
        self.figures: list[tuple[str, Path]] = []  # (src as written, resolved)

    # -- inline -------------------------------------------------------------

    def text(self, literal: str) -> str:
        return esc(literal)

    def code(self, body: str) -> str:
        return f"<code>{esc(body)}</code>"

    def kbd(self, body: str) -> str:
        return f"<kbd>{esc(body)}</kbd>"

    def linebreak(self) -> str:
        return "<br>"

    def link(self, href: str, ref: SectionRef | None, body: str) -> str:
        # `ref` is not consulted for the destination: the walker has already
        # checked that the section exists, and the href it checked is a
        # relative `.md#slug` that the rewrite turns into the right `.html`
        # anyway. Spelling it twice is how the two could disagree.
        dest = self.links.rewrite(href, self.src, self.page_url)
        offsite = ' class="external"' if "://" in dest else ""
        return f'<a href="{attr(dest)}"{offsite}>{body}</a>'

    def bold(self, body: str) -> str:
        return f"<strong>{body}</strong>"

    def em(self, body: str) -> str:
        return f"<em>{body}</em>"

    def xref(self, text: str, number: str) -> str:
        url = self.chapter_urls.get(number)
        if url is None:
            return esc(text)
        return f'<a href="{attr(SiteLinks.relative(url, self.page_url))}">{esc(text)}</a>'

    def mark(self, char: str) -> str:
        return esc(char)

    # -- blocks -------------------------------------------------------------

    def heading(self, level: int, body: str, label: SectionRef | None) -> str:
        # The page's own `<h1>` is set by the chrome from the chapter title, so
        # the deepest heading here is `##` and it maps to `<h2>` unchanged.
        if label is None:
            return f"<h{level}>{body}</h{level}>"
        self.headings.append((level, label.slug, body))
        anchor = f'<a class="anchor" href="#{attr(label.slug)}" aria-label="Permalink">#</a>'
        return f'<h{level} id="{attr(label.slug)}">{body}{anchor}</h{level}>'

    def figure(self, src: str, target: Path, caption: str) -> str:
        self.figures.append((src, target))
        return (
            f'<figure><img src="{attr(src)}" alt="{attr(strip_tags(caption))}" loading="lazy">'
            f"<figcaption>{caption}</figcaption></figure>"
        )

    def code_block(self, body: str, lang: str) -> str:
        cls = f' class="language-{attr(lang)}"' if lang else ""
        return f"<pre><code{cls}>{esc(body)}</code></pre>"

    def callout(self, kind: str, body: str) -> str:
        title = _CALLOUT_TITLES.get(kind, kind.title())
        return (
            f'<aside class="callout callout-{kind.lower()}">'
            f'<p class="callout-kind">{esc(title)}</p>{body}</aside>'
        )

    def locators(self, entries: list[tuple[SectionRef, str]]) -> str:
        # The PDF replaces these names with page numbers. On the web the name
        # *is* the locator -- there are no pages -- so the link text the
        # Markdown already carries is what gets set.
        links = []
        for ref, text in entries:
            url = self.links.pages.get(self._book_page(ref.stem))
            if url is None:
                links.append(text)
                continue
            dest = SiteLinks.relative(url, self.page_url) + f"#{ref.slug}"
            links.append(f'<a href="{attr(dest)}">{text}</a>')
        return ", ".join(links)

    def _book_page(self, stem: str) -> Path:
        """The source file a same-book section ref names."""
        return self.src.parent / f"{stem}.md"

    def table(
        self,
        header: list[str],
        rows: list[list[str]],
        aligns: list[str],
        kind: str | None,
    ) -> str:
        cls = f' class="{kind}"' if kind else ""

        def row(cells: list[str], tag: str) -> str:
            out = "".join(
                f'<{tag} class="ta-{aligns[i]}">{cell}</{tag}>' for i, cell in enumerate(cells)
            )
            return f"<tr>{out}</tr>"

        head = row(header, "th")
        body = "".join(row(r, "td") for r in rows)
        # A reference table is wider than a phone. Scrolling it inside its own
        # box is the only way the page itself does not scroll sideways.
        return (
            f'<div class="table-wrap"><table{cls}>'
            f"<thead>{head}</thead><tbody>{body}</tbody></table></div>"
        )

    def list_block(self, items: list[ListItem]) -> str:
        out: list[str] = []
        stack: list[tuple[int, str]] = []  # (indent width, tag)
        for item in items:
            if item.continuation:
                # A wrapped line of the item above, not an item of its own.
                out.append(" " + item.text)
                continue
            width = len(item.indent.expandtabs(4))
            tag = "ol" if item.ordered else "ul"
            while stack and width < stack[-1][0]:
                out.append(f"</li></{stack.pop()[1]}>")
            if not stack or width > stack[-1][0]:
                stack.append((width, tag))
                out.append(f"<{tag}>")
            else:
                out.append("</li>")
            out.append(f"<li>{item.text}")
        while stack:
            out.append(f"</li></{stack.pop()[1]}>")
        return "".join(out)

    def paragraph(self, body: str) -> str:
        return f"<p>{body}</p>"

    # -- checks -------------------------------------------------------------

    def check_prose(self, literal: str, path: Path, lineno: int) -> None:
        # Nothing to check: the en-dash rule build_book.py enforces is a Typst
        # markup artifact, and a browser prints `--config` as written.
        return


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------


@dataclass
class Site:
    """Everything the chrome needs that is not specific to one page."""

    books: list[Book]
    pages: dict[Path, str]
    links: SiteLinks
    repo: str
    version: str


def shell(
    site: Site,
    *,
    url: str,
    title: str,
    body: str,
    sidebar: str = "",
    hero: str = "",
    classes: str = "",
) -> str:
    """One complete page, chrome and all."""

    def rel(target: str) -> str:
        return attr(SiteLinks.relative(target, url))

    nav = "".join(
        '<a{here} href="{href}">{name}</a>'.format(
            here=' class="here"' if url.startswith(book.slug + "/") else "",
            href=rel(book.index_url),
            name=esc(book.name),
        )
        for book in site.books
    )
    banner = (
        f'<div class="banner">This site is built from <code>main</code>. '
        f"The newest release is "
        f'<a href="{attr(site.repo)}/releases/latest">v{esc(site.version)}</a>.</div>'
    )
    aside = f'<aside class="sidebar">{sidebar}</aside>' if sidebar else ""
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{esc(title)}</title>",
            f'<link rel="stylesheet" href="{rel("site.css")}">',
            f'<link rel="icon" href="{rel("assets/logo.png")}">',
            "</head>",
            f'<body class="{attr(classes)}">',
            banner,
            '<header class="topbar">',
            f'<a class="brand" href="{rel("index.html")}">c64cast</a>',
            f'<nav>{nav}<a class="external" href="{attr(site.repo)}">GitHub</a></nav>',
            "</header>",
            hero,
            '<div class="layout">',
            aside,
            f'<main class="content">{body}</main>',
            "</div>",
            '<footer class="sitefoot">',
            f'<a href="{attr(site.repo)}/blob/main/LICENSE">MIT</a> · '
            f'<a href="{attr(site.repo)}">source on GitHub</a>',
            "</footer>",
            "</body>",
            "</html>",
            "",
        ]
    )


def on_this_page(headings: list[tuple[int, str, str]]) -> str:
    """The current page's own sections, for the sidebar."""
    if not headings:
        return ""
    items = "".join(
        f'<li class="lvl{level}"><a href="#{attr(slug)}">{markup}</a></li>'
        for level, slug, markup in headings
        if level in (2, 3)
    )
    return f'<nav class="onthispage"><p class="navtitle">On this page</p><ul>{items}</ul></nav>'


def book_nav(site: Site, book: Book, url: str, headings: list[tuple[int, str, str]]) -> str:
    """The sidebar for a page inside a book: every chapter, this one expanded."""
    out = [
        f'<p class="navtitle"><a href="{attr(SiteLinks.relative(book.index_url, url))}">'
        f"{esc(book.name)}</a></p>",
        "<ul>",
    ]
    for chapter in book.chapters:
        target = book.page_url(chapter.path)
        here = target == url
        number = f'<span class="num">{esc(chapter.number)}</span> ' if chapter.number else ""
        mark = ' class="here"' if here else ""
        out.append(
            f"<li{mark}>{number}"
            f'<a href="{attr(SiteLinks.relative(target, url))}">{esc(chapter.title)}</a>'
        )
        if here:
            out.append(on_this_page(headings))
        out.append("</li>")
    out.append("</ul>")
    out.append(
        f'<p class="pdf"><a href="{attr(site.repo)}/{book.pdf}">Download the PDF</a></p>',
    )
    return "\n".join(out)


def pager(url: str, prev: tuple[str, str] | None, nxt: tuple[str, str] | None) -> str:
    """The links that let the guide be read straight through."""
    parts = ['<nav class="pager">']
    if prev:
        parts.append(f'<a class="prev" href="{attr(SiteLinks.relative(prev[0], url))}">')
        parts.append(f"<span>Previous</span>{esc(prev[1])}</a>")
    if nxt:
        parts.append(f'<a class="next" href="{attr(SiteLinks.relative(nxt[0], url))}">')
        parts.append(f"<span>Next</span>{esc(nxt[1])}</a>")
    parts.append("</nav>")
    return "".join(parts)


def edit_link(site: Site, src: Path) -> str:
    path = src.relative_to(REPO_ROOT).as_posix()
    return (
        f'<p class="editlink"><a class="external" href="{attr(site.repo)}/edit/main/{attr(path)}">'
        f"Edit this page on GitHub</a></p>"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_chapter(
    site: Site,
    book: Book,
    chapter: Chapter,
    prev: tuple[str, str] | None,
    nxt: tuple[str, str] | None,
) -> tuple[str, list[tuple[str, Path]]]:
    """One chapter page. Returns its HTML and the figures it needs copied."""
    url = book.page_url(chapter.path)
    emitter = HtmlEmitter(chapter.path, url, site.links, book.chapter_urls)
    # Walked again rather than reusing what `load_book` produced: an emitter
    # resolves links against the page it is emitting, so it cannot be built
    # until the page map exists -- and the map needs every book's chapters,
    # which is what that first pass was for.
    _, raw, offset = parse_front_matter(chapter.path.read_text(encoding="utf-8"), chapter.path)
    conv = Converter(chapter.path, offset, emitter, book.numbers, book.anchors)
    body = conv.convert(raw)

    label = f'<p class="eyebrow">{esc(chapter_label(chapter))}</p>' if chapter.number else ""
    return (
        shell(
            site,
            url=url,
            title=f"{chapter.title} — c64cast {book.name}",
            body=label
            + f"<h1>{esc(chapter.title)}</h1>"
            + body
            + pager(url, prev, nxt)
            + edit_link(site, chapter.path),
            sidebar=book_nav(site, book, url, emitter.headings),
            classes="book",
        ),
        emitter.figures,
    )


def chapter_label(chapter: Chapter) -> str:
    """ "Chapter 4" or "Appendix F" -- whichever the front matter's number means."""
    number = chapter.number or ""
    return f"{'Appendix' if number.isalpha() else 'Chapter'} {number}"


def render_book_index(site: Site, book: Book) -> str:
    """A book's contents page: every chapter, and the sections inside it."""
    url = book.index_url
    parts = [
        f"<h1>c64cast {esc(book.name)}</h1>",
        f'<p class="tagline">{esc(book.tagline)}</p>' if book.tagline else "",
        f'<p class="pdf"><a href="{attr(site.repo)}/{book.pdf}">Download the typeset PDF</a></p>',
        '<ol class="contents">',
    ]
    for chapter in book.chapters:
        target = SiteLinks.relative(book.page_url(chapter.path), url)
        eyebrow = (
            f'<span class="num">{esc(chapter_label(chapter))}</span>' if chapter.number else ""
        )
        sections = "".join(
            f'<li><a href="{attr(target)}#{attr(ref.slug)}">{title}</a></li>'
            for ref, title in chapter.sections
        )
        parts.append(
            f"<li>{eyebrow}"
            f'<a class="chapter" href="{attr(target)}">{esc(chapter.title)}</a>'
            + (f"<ul>{sections}</ul>" if sections else "")
            + "</li>"
        )
    parts.append("</ol>")
    return shell(
        site,
        url=url,
        title=f"c64cast {book.name}",
        body="".join(parts),
        sidebar=book_nav(site, book, url, []),
        classes="book contents-page",
    )


def render_standalone(site: Site, name: str) -> tuple[str, list[tuple[str, Path]]]:
    """One of the documents that belongs to no book."""
    src = DOCS / name
    url = site.pages[src]
    emitter = HtmlEmitter(src, url, site.links)
    _, raw, offset = parse_front_matter(src.read_text(encoding="utf-8"), src)
    # Its own sections and no others: a standalone document is not part of a
    # book, so a `#anchor` in it can only mean one of its own headings.
    anchors = frozenset(section_label(src.stem, slug) for slug in file_section_slugs(raw))
    conv = Converter(src, offset, emitter, frozenset(), anchors)
    body = conv.convert(raw)
    if conv.title is None:
        raise BookError(f"{src.relative_to(REPO_ROOT)}: no `# Title`")
    return (
        shell(
            site,
            url=url,
            title=f"{conv.title} — c64cast",
            body=f"<h1>{esc(conv.title)}</h1>{body}{edit_link(site, src)}",
            sidebar=on_this_page(emitter.headings),
            classes="standalone",
        ),
        emitter.figures,
    )


def readme_parts(text: str) -> tuple[str, str]:
    """The README's pitch paragraph and its body, split at the first `##`.

    Everything above that heading -- the logo, the badge row, the title -- is
    chrome the site draws itself, except the pitch, which is the best paragraph
    about what c64cast is and is rendered into the hero rather than rewritten.
    The file on disk is never touched: it is the PyPI long description.
    """
    lines = text.split("\n")
    body_at = next((i for i, line in enumerate(lines) if line.startswith("## ")), -1)
    if body_at < 0:
        raise BookError("README.md has no `## ` section for the site to start its body at")
    badges = [i for i, line in enumerate(lines[:body_at]) if line.startswith("[![")]
    if not badges:
        raise BookError("README.md has no badge row; the site reads the pitch as what follows it")
    pitch = "\n".join(lines[max(badges) + 1 : body_at]).strip()
    if not pitch:
        raise BookError("README.md has no pitch paragraph between the badges and the first `##`")
    return pitch, "\n".join(lines[body_at:])


def render_landing(site: Site) -> str:
    """The front page: a generated hero over the README's own body.

    The hero is generated rather than written because everything in it already
    exists and is already maintained -- the pitch is the README's, each card is
    a book's own `book.toml` -- and a fourth book should appear here without
    anyone remembering to add it.
    """
    url = "index.html"
    src = REPO_ROOT / "README.md"
    pitch, body_md = readme_parts(src.read_text(encoding="utf-8"))

    # Its own headings, so the README's `[…](#hardware-needed)` shortcuts
    # resolve here exactly as they do on github.com.
    anchors = frozenset(section_label(src.stem, slug) for slug in file_section_slugs(body_md))
    emitter = HtmlEmitter(src, url, site.links)

    def walk(markdown: str) -> str:
        conv = Converter(src, 1, emitter, frozenset(), anchors)
        conv.title = ""  # the site draws the title; the README's `#` is chrome
        return conv.convert(markdown)

    pitch_html = walk(pitch)
    body_html = walk(body_md)

    cards = "".join(
        f'<a class="bookcard" href="{attr(SiteLinks.relative(book.index_url, url))}">'
        f"<h3>{esc(book.name)}</h3>"
        f"<p>{esc(book.tagline)}</p>"
        f'<span class="cta">Read online</span></a>'
        for book in site.books
    )
    hero = (
        '<section class="hero">'
        # The logo *is* the page's title, so it is marked up as one rather than
        # leaving the front page the only one on the site without an `h1`.
        '<h1><img class="herologo" src="assets/logo.png" alt="c64cast"'
        ' width="800" height="271"></h1>'
        f'<div class="pitch">{pitch_html}</div>'
        f'<div class="bookcards">{cards}</div>'
        "</section>"
    )
    return shell(
        site,
        url=url,
        title="c64cast — turn a real Commodore 64 into a programmable display",
        body=body_html,
        hero=hero,
        classes="landing",
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def load_book(book: Book) -> None:
    """Read every chapter of a book, for its contents page and its sidebar."""
    paths = discover_chapters(book.dir)
    book.numbers = chapter_numbers(paths)
    book.anchors = section_anchors(paths)
    # A throwaway emitter: this pass is for the titles and section lists, and
    # each page is walked again with an emitter that knows its own URL.
    scratch = HtmlEmitter(book.dir, "", SiteLinks({}, ""))
    book.chapters = [load_chapter(p, scratch, book.numbers, book.anchors) for p in paths]


def build(out: Path, *, write: bool = True) -> int:
    books = discover_books()
    for book in books:
        load_book(book)

    pages = build_page_map(books)
    repo = repo_url()
    site = Site(
        books=books,
        pages=pages,
        links=SiteLinks(pages, repo),
        repo=repo,
        version=book_version(),
    )

    rendered: dict[str, str] = {"index.html": render_landing(site)}
    figures: list[tuple[str, Path, str]] = []  # (src as written, resolved, page url)

    for book in books:
        rendered[book.index_url] = render_book_index(site, book)
        stops = [(book.index_url, "Contents")] + [
            (book.page_url(c.path), c.title) for c in book.chapters
        ]
        for i, chapter in enumerate(book.chapters, start=1):
            html_text, figs = render_chapter(
                site,
                book,
                chapter,
                prev=stops[i - 1],
                nxt=stops[i + 1] if i + 1 < len(stops) else None,
            )
            url = book.page_url(chapter.path)
            rendered[url] = html_text
            figures += [(s, t, url) for s, t in figs]

    for name in STANDALONE:
        html_text, figs = render_standalone(site, name)
        url = pages[DOCS / name]
        rendered[url] = html_text
        figures += [(s, t, url) for s, t in figs]

    if not write:
        print(f"site source OK ({len(rendered)} pages)")
        return 0

    if out.exists():
        shutil.rmtree(out)
    for url, text in rendered.items():
        target = out / url
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    copy_assets(out, figures)
    print(f"wrote {out} ({len(rendered)} pages)")
    return 0


def copy_assets(out: Path, figures: list[tuple[str, Path, str]]) -> None:
    """Everything the pages point at that is not itself a page."""
    (out / "site.css").write_bytes(STYLESHEET.read_bytes())

    (out / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copy2(LOGO, out / "assets" / LOGO.name)

    fonts = out / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    # The licenses go with the faces because the OFL requires it, not as a
    # courtesy: a site that serves the TTF and not the license is redistributing
    # them out of compliance.
    for source in sorted(FONT_DIR.glob("*.ttf")) + sorted(FONT_DIR.glob("OFL-*.txt")):
        shutil.copy2(source, fonts / source.name)

    # A figure's `src` is relative to the page that draws it, so it is copied
    # to that same relative place rather than to one shared directory.
    for src, target, page_url in figures:
        dest = out / posixpath.normpath(posixpath.join(posixpath.dirname(page_url), src))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="site root (default: docs/_site)")
    ap.add_argument("--check", action="store_true", help="parse only; write nothing")
    args = ap.parse_args()

    try:
        return build(args.out, write=not args.check)
    except BookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
