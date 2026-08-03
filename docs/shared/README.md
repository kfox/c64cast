# Shared book assets

What every c64cast book is built from. A book is a directory under `docs/`
holding its prose as numbered Markdown plus a `book.toml`. There are three:
the [User's Guide](../guide/README.md), the [Programmer's Reference
Guide](../reference/README.md) and the [Performance Card](../card/README.md).

| File | Is |
|---|---|
| `template.typ` | The entire visual language — palette, faces, and one entry point per layout |
| `fonts/` | The two OFL faces, vendored, plus their [licences](fonts/README.md) |

Nothing here is book-specific. A change to `template.typ` restyles every book
at once, which is the point: they are a series, and a table in one should look
like a table in the next.

```
docs/<book>/*.md
      |
      +--> scripts/build_book.py --> docs/<book>/<output>.typ
                                         |
      docs/shared/template.typ  --------->+--> typst --> .pdf
```

`build_book.py` translates Markdown constructs; `template.typ` decides what
they look like. Each book's `book.toml` names the `layout` it takes:

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
