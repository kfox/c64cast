# Fonts

Third-party fonts, vendored so the User's Guide PDF renders identically from a
fresh checkout on any platform instead of depending on what happens to be
installed. `make guide` passes this directory to Typst with `--font-path`.

**These files are not covered by c64cast's MIT licence.** Both are licensed
under the [SIL Open Font License, Version 1.1](https://openfontlicense.org/),
whose full text and copyright notice sit alongside each font here.

| Font | Version | Copyright | Upstream | Licence |
|---|---|---|---|---|
| `Jost[wght].ttf`, `Jost-Italic[wght].ttf` | 3.710 | Copyright 2020 The Jost Project Authors | [indestructible-type/Jost](https://github.com/indestructible-type/Jost), via [google/fonts `ofl/jost`](https://github.com/google/fonts/tree/main/ofl/jost) | [`OFL-Jost.txt`](OFL-Jost.txt) |
| `Inconsolata[wdth,wght].ttf` | 3.001 | Copyright 2006 The Inconsolata Project Authors | [cyrealtype/Inconsolata](https://github.com/cyrealtype/Inconsolata), via [google/fonts `ofl/inconsolata`](https://github.com/google/fonts/tree/main/ofl/inconsolata) | [`OFL-Inconsolata.txt`](OFL-Inconsolata.txt) |

Both are the upstream files byte-for-byte, and each carries its copyright,
licence and licence URL in its own `name` table as well.

## If you touch anything in here

- **Keep the `OFL-*.txt` files with the fonts.** The licence requires the
  copyright notice and licence text to accompany every copy. Anything that
  redistributes the fonts — a release archive, a container image — has to
  carry them too.
- **Don't edit the fonts in place.** Neither declares a Reserved Font Name, so
  a modified version *may* keep the family name, but a silently-altered font
  under an unchanged name is a debugging trap. Replace with a new upstream
  version instead, and update the table above.
- **`pre-commit` skips this directory** (see the `exclude` in
  [`.pre-commit-config.yaml`](../../../.pre-commit-config.yaml)). The
  whitespace fixers happily reformat a licence file, which is not ours to
  reformat.

Why these two faces in particular — and why the original book's own body face
is neither used nor shipped — is in [`../README.md`](../README.md#fonts).
