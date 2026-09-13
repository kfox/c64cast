// c64cast books — the visual language shared by `guide()`, a bound book, and
// `card()`, a printable hand-out. scripts/build_book.py converts each book's
// Markdown to a .typ that imports this file.
//
// Trim 449.3 x 665.3 pt (6.24 x 9.24 in), accent #2B73B5, Jost* body and
// Inconsolata mono are measured from the Commodore 64 Ultimate User's Guide
// (1st Edition, October 2025) — an homage to its typographic system, not to
// its identity: c64cast's own name and logo, no Commodore branding.

#let accent = rgb("#2B73B5")
#let accent-pale = rgb("#9FC0DE")
#let accent-wash = rgb("#F2F6FA")
#let ink = rgb("#111111")
#let keycap-fill = rgb("#3A3A3A")

// C64 screen colors, for `basic` listings rendered as the machine shows them.
#let c64-blue = rgb("#4038AB")
#let c64-lightblue = rgb("#8078D8")

// Both faces are Open Font License and vendored in docs/shared/fonts/, which
// every book build passes to Typst with --font-path. Each names the other as
// its only fallback, and `fallback: false` in the layouts below cuts the
// machine's own fonts out of the chain, so nothing depends on what is installed
// on the build machine. Jost* is a free Futura-lineage stand-in for the
// original's commercial body face; Inconsolata is the original's own mono face
// and is itself OFL. Neither covers ✓ (U+2713) or → (U+2192) -- see `tick` and
// `rarrow` -- and a glyph in neither face is simply not drawn, so
// tests/test_book_fonts.py checks every character of every book against these
// two files.
#let body-font = ("Jost", "Inconsolata")
#let mono-font = ("Inconsolata", "Jost")

// Body size per layout: everything in elements() is a multiple of whichever of
// these its layout passes in.
#let body-size = 10pt
#let card-size = 8.5pt

// Tracked so the table of contents can render each entry's folio in the style
// that page actually uses: roman front matter, arabic body.
#let numstyle = state("guide-numstyle", "i")

// Whether the current chapter contributes its sections to the contents:
// front-matter chapters are listed as single lines.
#let toc-sections = state("guide-toc-sections", false)

#let kbd(body) = box(
  fill: keycap-fill,
  radius: 2.5pt,
  inset: (x: 4pt, y: 2pt),
  outset: (y: 2pt),
  text(fill: white, weight: "bold", size: 0.86em, body),
)

// ✓ and → are drawn rather than typed: neither is in Jost, and Inconsolata's
// are a thin right-leaning check and a mono arrow squeezed into a 0.5em slot,
// both foreign matter in a page of geometric sans. Stroked in `em`, so they
// scale with the text they sit in and take the weight of the face around them.
#let _mark-stroke = (paint: ink, thickness: 0.078em, cap: "round", join: "round")

// Rests on the baseline, like a capital.
#let tick = box(width: 0.66em, height: 0.6em, baseline: 0pt, {
  place(line(start: (4%, 52%), end: (36%, 94%), stroke: _mark-stroke))
  place(line(start: (34%, 94%), end: (96%, 6%), stroke: _mark-stroke))
})

// Centered on the x-height, where a typeset arrow sits, not on the baseline.
#let rarrow = box(width: 0.95em, height: 0.44em, baseline: -0.02em, {
  place(line(start: (2%, 50%), end: (94%, 50%), stroke: _mark-stroke))
  place(line(start: (60%, 6%), end: (97%, 50%), stroke: _mark-stroke))
  place(line(start: (60%, 94%), end: (97%, 50%), stroke: _mark-stroke))
})

#let callout(kind: "NOTE", body) = block(
  width: 100%,
  stroke: 0.7pt + accent,
  inset: (x: 9pt, y: 8pt),
  above: 11pt,
  below: 11pt,
  breakable: true,
  {
    set par(justify: true, first-line-indent: 0pt)
    text(weight: "bold")[#kind:]
    h(4pt)
    body
  },
)

#let screenshot(path, caption) = figure(
  block(
    stroke: 0.7pt + accent-pale,
    inset: 0pt,
    clip: true,
    image(path, width: 78%),
  ),
  caption: caption,
  supplement: none,
  numbering: none,
  kind: "screen",
)

// A field's name, type and default stack into one fixed column and the
// description takes the rest. Fixed rather than `auto` because an `auto` column
// is sized to the longest entry in *its own* table, and these appendices read
// as one list, so the left margin moved every second page. 1.5in holds the 95th
// percentile of names (19 characters), types (14) and defaults (15) on one line
// at the mono size.
#let fields-column = 1.5in
#let fields-table(..args) = table(columns: (fields-column, 1fr), ..args)

// On paper an index locator is a page, not the section title the index's
// Markdown links to. The folio is set in the numbering style read at the
// *target*, so a locator in the front matter prints iv and one in the body 84.
// Duplicates are dropped: three sections discussing one term can share a page.
#let pagerefs(targets) = context {
  let seen = ()
  for target in targets {
    let found = query(target)
    if found.len() == 0 { continue }
    let loc = found.first().location()
    let folio = numbering(numstyle.at(loc), ..counter(page).at(loc))
    if folio in seen { continue }
    if seen.len() > 0 { [, ] }
    seen.push(folio)
    link(loc, folio)
  }
}

#let chapter(number: none, title: "", contents: ()) = {
  if number == none {
    toc-sections.update(false)
    pagebreak(weak: true)
    [#metadata((kind: "front", title: title)) <guide-toc>]
    block(above: 0pt, below: 15pt, text(
      fill: accent,
      weight: "bold",
      size: 15pt,
      tracking: 0.3pt,
      hyphenate: false,
      upper(title),
    ))
  } else {
    toc-sections.update(true)
    pagebreak(weak: true, to: "odd")
    page(
      fill: accent,
      margin: (left: 0.85in, right: 0.85in, top: 1.35in, bottom: 1in),
      header: none,
      // `footer: none` is what drops the folio; the page's `numbering` is left
      // alone, because that is what fills the PDF reader's thumbnail labels,
      // and suppressing it too left every opener as a gap in the strip.
      footer: none,
      {
        // Inside the opener page, so the contents entry points at the opener
        // rather than at the first page of body copy.
        [#metadata((kind: "chapter", number: number, title: title)) <guide-toc>]
        // What "Appendix F" in the prose jumps to, built with `label()` because
        // `<ch-F>` cannot be written literally in a function every chapter
        // shares.
        [#metadata(number)#label("ch-" + number)]
        set text(fill: white)
        // Named `kind` and not `label`: the section list below calls `label()`,
        // and a local binding of that name shadows the function for the rest of
        // the block.
        let kind = if regex("^[0-9]") in number { "CHAPTER" } else { "APPENDIX" }
        align(right, box({
          text(size: 17pt, weight: "bold", tracking: 1.2pt)[#kind]
          h(12pt)
          text(size: 66pt, weight: "bold")[#number]
        }))
        v(32pt)
        // A title that wraps stays ragged: the document-wide `justify` would
        // space "WHEN SOMETHING GOES" across the full measure, and hyphenation
        // would break "DISPLAY" as "DIS-PLAY".
        align(right, block(width: 100%, {
          set par(justify: false, leading: 0.55em)
          set text(hyphenate: false)
          align(right, text(size: 20pt, weight: "bold", tracking: 0.4pt, upper(title)))
        }))
        v(64pt)
        pad(left: 38%, {
          set text(size: 10pt)
          set par(leading: 0.75em, justify: false)
          for entry in contents {
            // The fill is set on the link's *body* rather than around the list:
            // the document-wide show rule wraps a label link in
            // `text(fill: accent, ..)`, which outranks an inherited white and
            // left every title invisible against this page.
            block(above: 6.5pt, below: 6.5pt)[
              #box(baseline: -0.8pt, circle(radius: 1.5pt, fill: white))
              #link(label(entry.label), text(fill: white, entry.title))
            ]
          }
        })
      },
    )
  }
}

// Built from the <guide-toc> metadata that chapter() and the level-2 heading
// show rule leave behind.
#let toc() = {
  // The original sets its contents page noticeably lower than a body page.
  v(0.5in)
  block(above: 0pt, below: 22pt, text(
    fill: accent,
    weight: "bold",
    size: 15pt,
    tracking: 0.3pt,
  )[TABLE OF CONTENTS])

  context {
    let gutter = 0.3in
    let folio = 0.42in

    for item in query(<guide-toc>) {
      let d = item.value
      let loc = item.location()
      let pg = numbering(numstyle.at(loc), ..counter(page).at(loc))

      if d.kind == "chapter" {
        block(above: 17pt, below: 7pt, grid(
          columns: (gutter, 1fr, folio),
          text(weight: "bold")[#d.number],
          link(loc, text(weight: "bold", upper(d.title))),
          align(right, link(loc, text(weight: "bold")[#pg])),
        ))
      } else {
        block(above: 5pt, below: 5pt, grid(
          columns: (gutter, 1fr, folio),
          [],
          link(loc, {
            d.title
            h(5pt)
            box(width: 1fr, repeat[#h(3.4pt).#h(3.4pt)])
          }),
          align(right, link(loc)[#pg]),
        ))
      }
    }
  }
}

// `frontmatter` and `mainmatter` both take the rest of the document and are
// applied with `#show: <fn>` rather than called as `#frontmatter()`: a
// `set page` inside a function body is scoped to that body, and the called
// form left the PDF's own page labels on the document-level "i" while the
// footer looked correct.

// The roman count is already running (it starts at the half-title, so the
// colophon lands on ii); this only marks where the numbered front matter
// begins.
#let frontmatter(body) = {
  set page(numbering: "i")
  numstyle.update("i")
  body
}

// Break to the recto *first*, then restart the count, so chapter 1's opener is
// page 1: resetting before the break lets the blank verso consume the low
// numbers, and the contents then point at page 3.
#let mainmatter(body) = {
  pagebreak(weak: true, to: "odd")
  set page(numbering: "1")
  numstyle.update("1")
  counter(page).update(1)
  body
}

#let cover(title: "", volume: "", subtitle: "", tagline: "", logo: none, version: "") = page(
  fill: gradient.linear(accent.darken(22%), accent, accent.lighten(8%), angle: 90deg),
  margin: 0pt,
  header: none,
  footer: none,
  numbering: none,
  {
    // A faint grid, echoing the original cover's plotted background.
    place(top + left, block(width: 100%, height: 100%, {
      for i in range(1, 14) {
        place(top + left, dx: i * 7.14%, line(
          start: (0pt, 0pt),
          end: (0pt, 100%),
          stroke: 0.5pt + rgb(255, 255, 255, 28),
        ))
      }
      for i in range(1, 20) {
        place(top + left, dy: i * 5%, line(
          start: (0pt, 0pt),
          end: (100%, 0pt),
          stroke: 0.5pt + rgb(255, 255, 255, 28),
        ))
      }
    }))

    set text(fill: white)
    place(top + center, dy: 1.5in, block(width: 78%, {
      // Nothing on a cover justifies or hyphenates: "PROGRAMMER'S REFERENCE"
      // was spaced across the full measure and broken as "REFER-ENCE". Set here
      // rather than around the one line, so no wrapper is introduced and the
      // guide's cover keeps its exact geometry.
      set par(justify: false)
      set text(hyphenate: false)
      if logo != none { image(logo, width: 100%) }
      v(30pt)
      text(size: 26pt, weight: "bold", tracking: 1.2pt)[#upper(volume)]
      v(2pt)
      text(size: 12pt)[#subtitle]
    }))

    place(bottom + center, dy: -1.1in, text(size: 11pt, tracking: 0.5pt)[#tagline])

    if version != "" {
      place(
        bottom + center,
        dy: -0.72in,
        text(size: 8.5pt, tracking: 0.8pt, fill: rgb(255, 255, 255, 190))[
          VERSION #version
        ],
      )
    }
  },
)

#let half-title(title: "") = page(
  fill: accent,
  margin: (x: 0.85in, y: 1.6in),
  header: none,
  footer: none,
  numbering: none,
  {
    set text(fill: white)
    place(top + center, dy: 1.6in, block(width: 100%, align(center, text(
      size: 19pt,
      weight: "bold",
      tracking: 0.6pt,
    )[#upper(title)])))
  },
)

#let colophon(body) = page(
  header: none,
  {
    place(bottom + left, block(width: 100%, {
      set text(size: 9pt)
      // Roughly a blank line between paragraphs: a stack of unrelated legal
      // notices wants more separation than body copy.
      set par(justify: true, leading: 0.62em, spacing: 1.5em)
      body
    }))
  },
)

// How a heading, a listing, a table or a figure is set, shared by every layout.
// Every size and gap is a multiple of `size`, the body size its layout passes
// in — required rather than defaulted, so a new layout cannot silently inherit
// the book's — rather than an absolute the 10pt book happened to want: at the
// card's 8.5pt a 12.5pt section heading shouted and a 4.5pt row inset spent a
// column it did not have. A multiple of the parameter and not `em`, because a
// heading's own text size is already scaled when the show rule runs, so an `em`
// inside one compounds.
#let elements(size, body) = {
  // Level 1 is never used directly; chapter() draws the openers.
  show heading.where(level: 2): it => {
    context {
      if toc-sections.get() {
        [#metadata((kind: "section", title: it.body)) <guide-toc>]
      }
    }
    block(above: 1.8 * size, below: 0.9 * size, text(
      fill: accent,
      weight: "bold",
      size: 1.25 * size,
      tracking: 0.3pt,
      hyphenate: false,
      upper(it.body),
    ))
  }

  show heading.where(level: 3): it => block(
    above: 1.4 * size,
    below: 0.7 * size,
    text(weight: "bold", size: 1.15 * size, hyphenate: false, it.body),
  )

  show heading.where(level: 4): it => block(
    above: 1.1 * size,
    below: 0.5 * size,
    text(weight: "bold", size: size, hyphenate: false, it.body),
  )

  // No syntax highlighting: the original sets all of its listings in one color.
  set raw(theme: none)

  // Inline code is set slightly larger than the prose around it. The two faces
  // agree on x-height (Jost 0.460em, Inconsolata 0.457em) but not above it:
  // Inconsolata's ascenders reach 0.665em against Jost's 0.780em and its caps
  // and figures 0.623em against 0.700em, so mono sat visibly low against its
  // sentence at an equal nominal size. 1.08 splits x-height parity (1.00) and
  // cap parity (1.12). Relative, not absolute, so a flag quoted in a 9pt
  // colophon scales with it.
  show raw.where(block: false): it => text(font: mono-font, size: 1.08em, it)

  show raw.where(block: true): it => block(
    width: 100%,
    fill: accent-wash,
    inset: (x: 0.9 * size, y: 0.8 * size),
    radius: 2pt,
    above: 1.1 * size,
    below: 1.1 * size,
    breakable: true,
    {
      set par(justify: false, leading: 0.55em)
      // Body size, not below it: the two faces agree almost exactly on x-height
      // (Jost 0.460em, Inconsolata 0.457em), so equal nominal sizes look equal.
      set text(font: mono-font, size: size)
      it
    },
  )

  // Bold marks the names of things you click and type, and breaking "Enabled"
  // as "En-abled" makes a label look like prose.
  show strong: set text(hyphenate: false)

  // Blue marks a link the reader can follow away from the sentence. A link to a
  // *location* is the contents page, where every line is a pointer already --
  // coloring those turns the whole page blue and says nothing.
  show link: it => if type(it.dest) == location { it } else { text(fill: accent, it) }

  // The marker is drawn rather than typed: Jost's • is small and sits high in
  // its em box, so at body size it reads as a middot near the cap line. The
  // baseline offset centers the circle on the x-height.
  set list(
    indent: size,
    body-indent: 0.6 * size,
    spacing: 0.9em,
    marker: box(baseline: -0.8pt, circle(radius: 1.5pt, fill: ink)),
  )
  set enum(indent: size, body-indent: 0.6 * size, spacing: 0.9em)

  set table(
    stroke: (x, y) => if y == 0 { (bottom: 0.7pt + ink) } else { none },
    inset: (x: 0pt, y: 0.45 * size),
    column-gutter: 1.2 * size,
  )
  show table.cell.where(y: 0): set text(weight: "bold")

  // No cell justifies: a table inherits the document's justified body copy, and
  // a 1.6in column of names came out as two words a line with a river between
  // them.
  show table.cell: set par(justify: false)

  show figure: it => block(above: 1.3 * size, below: 1.3 * size, align(center, {
    it.body
    v(4pt)
    text(size: 0.85 * size)[#it.caption.body]
  }))

  body
}

#let guide(
  title: "",
  volume: "",
  subtitle: "",
  tagline: "",
  logo: none,
  pdf-title: "",
  version: "",
  body,
) = {
  set document(
    title: if version == "" { pdf-title } else { pdf-title + " " + version },
    keywords: ("c64cast", "Commodore 64", pdf-title, version),
  )

  set page(
    width: 6.24in,
    height: 9.24in,
    margin: (left: 0.82in, right: 0.82in, top: 0.86in, bottom: 0.86in),
    numbering: "i",
    number-align: center,
    footer: context {
      let n = counter(page).get()
      align(center, text(size: 8.5pt, weight: "bold")[
        #numbering(numstyle.get(), ..n)
      ])
    },
  )

  set text(
    font: body-font,
    fallback: false,
    size: body-size,
    fill: ink,
    lang: "en",
    hyphenate: true,
  )
  // At 0.85em the paragraph spacing sat close enough to the 0.62em leading that
  // a paragraph break read like a line break.
  set par(justify: true, leading: 0.62em, spacing: 1.25em)
  show: elements.with(body-size)

  cover(
    title: title,
    volume: volume,
    subtitle: subtitle,
    tagline: tagline,
    logo: logo,
    version: version,
  )
  // The cover is not a numbered page: restarting here makes the half-title i
  // and the colophon -- the first page to print a folio -- ii.
  counter(page).update(1)
  half-title(title: title)

  body
}

// A card has no room for full-page openers, so a "chapter" is a banded heading,
// and the section list an opener would carry is the next inch of the same page.
#let card-chapter(number: none, title: "", contents: ()) = {
  // The same cross-reference anchor a bound chapter carries.
  if number != none {
    [#metadata(number)#label("ch-" + number)]
  }
  block(
    width: 100%,
    fill: accent,
    inset: (x: 7pt, y: 5pt),
    above: 15pt,
    below: 9pt,
    text(fill: white, weight: "bold", size: 11pt, tracking: 0.4pt, upper(title)),
  )
}

#let card(title: "", subtitle: "", pdf-title: "", version: "", body) = {
  set document(
    title: if version == "" { pdf-title } else { pdf-title + " " + version },
    keywords: ("c64cast", "Commodore 64", "reference card", version),
  )

  set page(
    paper: "us-letter",
    margin: 0.5in,
    // A key set across seven inches of letter paper is a line the eye travels
    // rather than glances at, and two columns halve the page count.
    columns: 2,
    numbering: none,
    header: none,
    // The title block is a footer rather than a header, so it does not push the
    // first table down the page.
    footer: context {
      set text(size: 7.5pt)
      grid(
        columns: (1fr, auto),
        text(weight: "bold", fill: accent)[#upper(title) — #subtitle],
        align(right)[#version],
      )
    },
  )

  set text(
    font: body-font,
    fallback: false,
    size: card-size,
    fill: ink,
    lang: "en",
    hyphenate: false,
  )
  set par(justify: false, leading: 0.55em, spacing: 0.7em)
  show: elements.with(card-size)

  body
}
