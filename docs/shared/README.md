# Shared book assets

What every c64cast book is built from. A book is a directory under `docs/`
holding its prose as numbered Markdown plus a `book.toml`. There are three:
the [User's Guide](../guide/README.md), the [Programmer's Reference
Guide](../reference/README.md) and the [Performance Card](../card/README.md).

| File | Is |
|---|---|
| `template.typ` | The PDF's entire visual language — palette, faces, and one entry point per layout |
| `site.css` | The documentation site's, in the same palette and the same faces |
| `fonts/` | The two OFL faces, vendored, plus their [licenses](fonts/README.md) |

Nothing here is book-specific. A change to `template.typ` restyles every book
at once, which is the point: they are a series, and a table in one should look
like a table in the next. `site.css` makes the same bargain for the web, and
takes its palette from `template.typ`'s own declarations so a reader who
downloads the PDF after reading it online gets the same object.

```
                          scripts/bookdoc.py
                     (the dialect, and every check)
                                  |
docs/<book>/*.md -----------------+
      |                           |
      +--> scripts/build_book.py --> docs/<book>/<output>.typ
      |                                  |
      |    docs/shared/template.typ ----->+--> typst --> .pdf
      |
      +--> scripts/build_site.py --> docs/_site/<book>/*.html
                                         |
           docs/shared/site.css --------->+--> GitHub Pages
```

One reading of the Markdown, two renderings of it. `bookdoc.py` recognizes
each construct and checks it — an anchor that resolves nowhere, a chapter
cross-reference the book does not have — and each builder supplies only an
`Emitter` saying what its own output looks like. So a link written once lands
in the same place on github.com, in the PDF and on the site.

Each book's `book.toml` names the `layout` it takes:

| Layout | For | Gets |
|---|---|---|
| `guide` | A bound book | Cover, colophon, contents, full-page chapter openers |
| `card` | A printable hand-out | None of that — it opens on its first line |

Paths are spelled from the repository root (`/docs/guide/img/fig-1-1.png`), and
`typst compile` is passed `--root .` to match. Typst resolves a relative path
against the file the call is written in, and every `image()` call is written
*here* — so a book-relative path would be looked for next to this template.

See [the User's Guide README](../guide/README.md) for how to write for a book,
and for why these two faces.
