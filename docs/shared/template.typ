// c64cast books — the entire visual language lives in this file.
//
// Every book's prose is Markdown (docs/<book>/*.md). scripts/build_book.py
// converts it to a .typ that imports this template; nothing about the *look*
// is decided in the converter or in the prose. Tune the design here.
//
// Two entry points, both drawing on the same palette, faces and element
// styling: `guide()` for a bound book, `card()` for a printable hand-out.
//
// The house style is modelled on the Commodore 64 Ultimate User's Guide
// (1st Edition, October 2025). Values below were measured from that PDF
// rather than guessed:
//
//   trim         449.3 x 665.3 pt  = 6.24 x 9.24 in
//   accent       #2B73B5 — one blue, used for every accent in the book
//   body face    Jost* (see "Fonts" below)
//   mono face    Inconsolata
//
// This is an homage to that book's typographic system, not to its identity:
// c64cast's own name and logo, no Commodore wordmark or branding.

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

#let accent = rgb("#2B73B5")       // the one blue
#let accent-pale = rgb("#9FC0DE")  // figure frames, hairlines
#let accent-wash = rgb("#F2F6FA")  // code block fill
#let ink = rgb("#111111")          // body text
#let keycap-fill = rgb("#3A3A3A")  // <kbd> chips

// C64 screen colours, for `basic` listings rendered as the machine shows them.
#let c64-blue = rgb("#4038AB")
#let c64-lightblue = rgb("#8078D8")

// ---------------------------------------------------------------------------
// Fonts
// ---------------------------------------------------------------------------
//
// Both faces are Open Font License and vendored in docs/shared/fonts/, which
// every book build passes to Typst with --font-path. Nothing here depends on
// what happens to be installed on the machine doing the build, so the PDF
// looks the same from a fresh checkout on any platform.
//
// The original's body face, MegaGlacial, is commercial and is deliberately
// NOT used or named here. Jost* is a free geometric sans in the Futura
// lineage, which is where MegaGlacial sits too: single-story `a`, circular
// bowls, near-uniform stroke. Inconsolata IS the original's mono face, and is
// itself OFL, so that one is an exact match rather than a substitution.
//
// Named without fallbacks on purpose. A fallback chain would let a build that
// forgot --font-path quietly substitute a different face and produce a PDF
// that looks wrong in a way nobody notices; Typst instead warns that the
// family is unknown, which is the signal you want.
#let body-font = "Jost"
#let mono-font = "Inconsolata"

// Body size per layout. The card is set smaller because it is a hand-out read
// at a glance, not a book read in an armchair — and because everything in
// elements() is a multiple of whichever of these its layout passes in.
#let body-size = 10pt
#let card-size = 8.5pt

// ---------------------------------------------------------------------------
// Page-numbering style, tracked so the table of contents can render each
// entry's folio in the style that page actually uses (roman front matter,
// arabic body).
// ---------------------------------------------------------------------------

#let numstyle = state("guide-numstyle", "i")

// Whether the current chapter contributes its sections to the contents.
// Front-matter chapters (Quick Start, Fun Things to Try!, Introduction) are
// listed as single lines, exactly as in the original.
#let toc-sections = state("guide-toc-sections", false)

// ---------------------------------------------------------------------------
// Inline elements
// ---------------------------------------------------------------------------

// A key on the keyboard: <kbd>RETURN</kbd>
#let kbd(body) = box(
  fill: keycap-fill,
  radius: 2.5pt,
  inset: (x: 4pt, y: 2pt),
  outset: (y: 2pt),
  text(fill: white, weight: "bold", size: 0.86em, body),
)

// ---------------------------------------------------------------------------
// Callout boxes — NOTE:, TIP:, WARNING:
//
// The original draws a thin accent rule around white fill, with the label set
// bold and running into the first sentence.
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Figures — a captured C64 screen in a pale frame, with a caption below.
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Chapter openers
//
// A numbered chapter gets the full-bleed blue page: right-aligned CHAPTER
// label with a giant numeral, the title in bold white, then the section list.
// A front-matter chapter (number: none) gets a plain blue heading on a normal
// page, which is what the original does for Quick Start / Introduction.
// ---------------------------------------------------------------------------

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
      footer: none,
      numbering: none,
      {
        // Registered inside the opener page so the contents entry points at
        // the opener rather than at the first page of body copy.
        [#metadata((kind: "chapter", number: number, title: title)) <guide-toc>]
        set text(fill: white)
        // A lettered number is an appendix, not a chapter.
        let label = if regex("^[0-9]") in number { "CHAPTER" } else { "APPENDIX" }
        align(right, box({
          text(size: 17pt, weight: "bold", tracking: 1.2pt)[#label]
          h(12pt)
          text(size: 66pt, weight: "bold")[#number]
        }))
        v(32pt)
        // A title that wraps must stay ragged: the document-wide `justify`
        // would space "WHEN SOMETHING GOES" across the full measure, and
        // hyphenation would break "DISPLAY" as "DIS-PLAY".
        align(right, block(width: 100%, {
          set par(justify: false, leading: 0.55em)
          set text(hyphenate: false)
          align(right, text(size: 20pt, weight: "bold", tracking: 0.4pt, upper(title)))
        }))
        v(64pt)
        // The section list sits in from the left, as in the original.
        pad(left: 38%, {
          set text(size: 10pt)
          set par(leading: 0.75em, justify: false)
          for entry in contents {
            // Drawn, for the same reason as the body-list marker below.
            block(above: 6.5pt, below: 6.5pt)[
              #box(baseline: -0.8pt, circle(radius: 1.5pt, fill: white)) #entry
            ]
          }
        })
      },
    )
  }
}

// ---------------------------------------------------------------------------
// Table of contents
//
// Built by querying the metadata that chapter() and the level-2 heading show
// rule leave behind, so it can render chapter numbers in a left gutter and
// folios in whichever numbering style that page uses.
// ---------------------------------------------------------------------------

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
          text(weight: "bold", upper(d.title)),
          align(right, text(weight: "bold")[#pg]),
        ))
      } else {
        // Front-matter entries and chapter sections share an indent, with
        // leader dots running out to the folio.
        block(above: 5pt, below: 5pt, grid(
          columns: (gutter, 1fr, folio),
          [],
          {
            d.title
            h(5pt)
            box(width: 1fr, repeat[#h(3.4pt).#h(3.4pt)])
          },
          align(right)[#pg],
        ))
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Front matter / body numbering switches
// ---------------------------------------------------------------------------

// The roman count is already running by the time this is called -- it starts
// at the half-title so the colophon lands on ii, the way a printed book does.
// This only marks where the numbered front matter begins.
#let frontmatter() = {
  set page(numbering: "i")
  numstyle.update("i")
}

// Break to the recto *first*, then restart the count, so that chapter 1's
// opener page is page 1. Resetting before the break would let the blank verso
// consume the low numbers, and the contents would point at page 3.
#let mainmatter() = {
  pagebreak(weak: true, to: "odd")
  set page(numbering: "1")
  numstyle.update("1")
  counter(page).update(1)
}

// ---------------------------------------------------------------------------
// Cover, half-title and colophon
// ---------------------------------------------------------------------------

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
      // Nothing on a cover justifies or hyphenates. It only started to matter
      // with a second book: "USER'S GUIDE" fits one line, and
      // "PROGRAMMER'S REFERENCE" was spaced across the full measure and broken
      // as "REFER-ENCE". Set here rather than around the one line, so no
      // wrapper is introduced and the guide's cover keeps its exact geometry.
      set par(justify: false)
      set text(hyphenate: false)
      if logo != none { image(logo, width: 100%) }
      v(30pt)
      // Which volume of the series this is. From the book, not a literal: the
      // logo carries the wordmark, and this line is the only thing on the
      // cover that distinguishes one book from the next.
      text(size: 26pt, weight: "bold", tracking: 1.2pt)[#upper(volume)]
      v(2pt)
      text(size: 12pt)[#subtitle]
    }))

    place(bottom + center, dy: -1.1in, text(size: 11pt, tracking: 0.5pt)[#tagline])

    // Which release this printing documents. Set below the tagline and dimmed
    // against the gradient: a reader needs it when their copy and their install
    // disagree, and never before that, so it should not compete with the title.
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

// The colophon is the first page that prints a folio, and it sits on the
// bottom margin rather than floating above it -- a copyright page is a
// footnote to the book, not a spread of its own.
#let colophon(body) = page(
  header: none,
  {
    place(bottom + left, block(width: 100%, {
      set text(size: 9pt)
      // Roughly a blank line between paragraphs: this page is a stack of
      // unrelated legal notices, so it wants more separation than body copy.
      set par(justify: true, leading: 0.62em, spacing: 1.5em)
      body
    }))
  },
)

// ---------------------------------------------------------------------------
// Element styling
//
// Everything below the page: how a heading, a listing, a table or a figure is
// set. Shared by every layout, so a table in the card looks like a table in
// the guide and the two read as one series.
//
// Every size and gap here is a multiple of `size`, the body size its layout
// passes in — required rather than defaulted, so a new layout cannot silently
// inherit the book's — rather than an absolute the 10pt book happened to want: at the
// card's 8.5pt a 12.5pt section heading shouted and a 4.5pt row inset spent a
// column it did not have. Multiples of a parameter and not `em`, because a
// heading's own text size is already scaled when the show rule runs, so an
// `em` inside one compounds.
// ---------------------------------------------------------------------------

#let elements(size, body) = {
  // Headings ---------------------------------------------------------------
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

  // Code -------------------------------------------------------------------
  // No syntax highlighting: the original sets all of its listings in one
  // colour, and coloured tokens read as a different book.
  set raw(theme: none)

  // Inline code is set plain, with no tint behind it -- the original sets its
  // inline literals (paths, URLs) as bare mono, and a chip per flag would
  // speckle a page that mentions a lot of them.
  // Relative, not absolute, so a flag quoted in a 9pt colophon scales with it.
  show raw.where(block: false): it => text(font: mono-font, size: 1em, it)

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
      // Code is set at the body size, not below it, because these two faces
      // happen to agree almost exactly on x-height: Jost 0.460em, Inconsolata
      // 0.457em. Equal nominal sizes therefore look equal on the page, and
      // code no longer reads as fine print the way it did against a face with
      // a taller lowercase.
      set text(font: mono-font, size: size)
      it
    },
  )

  // Bold is what this book uses for the names of things you click and type —
  // menu entries, settings, values. Breaking "Enabled" as "En-abled" makes a
  // label look like prose, so strong text never hyphenates.
  show strong: set text(hyphenate: false)

  // Links ------------------------------------------------------------------
  show link: it => text(fill: accent, it)

  // Lists ------------------------------------------------------------------
  // Drawn rather than typed. Jost's • glyph is small and sits high in its em
  // box, so at body size it reads as a middot floating near the cap line. A
  // circle is font-independent: same mark whatever the body face becomes, and
  // the baseline offset centres it on the x-height.
  set list(
    indent: size,
    body-indent: 0.6 * size,
    spacing: 0.9em,
    marker: box(baseline: -0.8pt, circle(radius: 1.5pt, fill: ink)),
  )
  set enum(indent: size, body-indent: 0.6 * size, spacing: 0.9em)

  // Tables — bold header, one hairline beneath it, nothing else. ------------
  set table(
    stroke: (x, y) => if y == 0 { (bottom: 0.7pt + ink) } else { none },
    inset: (x: 0pt, y: 0.45 * size),
    column-gutter: 1.2 * size,
  )
  show table.cell.where(y: 0): set text(weight: "bold")

  // Figures ----------------------------------------------------------------
  show figure: it => block(above: 1.3 * size, below: 1.3 * size, align(center, {
    it.body
    v(4pt)
    text(size: 0.85 * size)[#it.caption.body]
  }))

  body
}

// ---------------------------------------------------------------------------
// Document shell — the bound book
// ---------------------------------------------------------------------------

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
  // The version rides in the PDF metadata as well as on the cover, so a file
  // that has been renamed, mailed around or printed can still be identified
  // from Get Info / `pdfinfo` without opening it to page one.
  set document(
    title: if version == "" { pdf-title } else { pdf-title + " " + version },
    // The book names itself rather than a literal: two books take this layout,
    // and the second one is not the User's Guide.
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

  set text(font: body-font, size: body-size, fill: ink, lang: "en", hyphenate: true)
  set par(justify: true, leading: 0.62em, spacing: 0.85em)
  show: elements.with(body-size)

  // Front matter -----------------------------------------------------------
  cover(
    title: title,
    volume: volume,
    subtitle: subtitle,
    tagline: tagline,
    logo: logo,
    version: version,
  )
  // The cover is not a numbered page. Restart the count here so the half-title
  // is i and the colophon -- the first page to actually print a folio -- is ii.
  counter(page).update(1)
  half-title(title: title)

  body
}

// ---------------------------------------------------------------------------
// Document shell — the printable card
//
// A hand-out, not a book: no cover, no colophon, no contents, and a page that
// comes out of whatever paper is in the tray. It is meant to live next to the
// controller, so it is set small and tight — the reader is glancing at it
// mid-performance, not reading it in an armchair.
// ---------------------------------------------------------------------------

// A card has no room for full-page openers, so a "chapter" is a banded
// heading. The section list an opener would carry is redundant here: it is
// the next inch of the same page.
#let card-chapter(number: none, title: "", contents: ()) = block(
  width: 100%,
  fill: accent,
  inset: (x: 7pt, y: 5pt),
  above: 15pt,
  below: 9pt,
  text(fill: white, weight: "bold", size: 11pt, tracking: 0.4pt, upper(title)),
)

#let card(title: "", subtitle: "", pdf-title: "", version: "", body) = {
  set document(
    title: if version == "" { pdf-title } else { pdf-title + " " + version },
    keywords: ("c64cast", "Commodore 64", "reference card", version),
  )

  set page(
    paper: "us-letter",
    margin: 0.5in,
    // Two columns, because a card is nearly all two-column tables and a key
    // set across seven inches of letter paper is a line the eye has to travel
    // rather than glance at. It also halves the page count, which is the
    // difference between one sheet printed double-sided and a stapled set.
    columns: 2,
    numbering: none,
    header: none,
    // The title block is a footer rather than a header so it does not push the
    // first table down the page, and it carries the version because a card
    // pinned to a desk outlives the release it was printed for.
    footer: context {
      set text(size: 7.5pt)
      grid(
        columns: (1fr, auto),
        text(weight: "bold", fill: accent)[#upper(title) — #subtitle],
        align(right)[#version],
      )
    },
  )

  set text(font: body-font, size: card-size, fill: ink, lang: "en", hyphenate: false)
  set par(justify: false, leading: 0.55em, spacing: 0.7em)
  show: elements.with(card-size)

  body
}
