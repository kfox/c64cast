# Changelog fragments

Each user-visible change adds one file here, so no two pull requests edit the
same file. Cutting a release (`scripts/bump_version.py`, see
[RELEASING.md](../RELEASING.md)) collects every fragment into the new version's
section of `CHANGELOG.md` and deletes the files. `## [Unreleased]` in
`CHANGELOG.md` only points here.

## Name

`<slug>.<category>.md`

- **slug**: lowercase letters, digits and single hyphens, unique in this
  directory. A short description of the change (`dac-curve-auto-armsid`); a
  PR number prefix is fine. Fragments of one category are published in slug
  order.
- **category**: one of `highlights`, `upgrade-notes`, `added`, `changed`,
  `deprecated`, `removed`, `fixed`, `security`.

A malformed name, an unknown category or an empty file fails
`python scripts/bump_version.py --check <version>` and the release tests.

## Body

The entry exactly as it should read in the changelog: one Markdown bullet,
starting with `- `, continuation lines indented two spaces. Lead with a bold
sentence that says what a user sees; the rest explains. A heading line inside
an entry is refused, because it would split the entry out of its category.

```markdown
- **`--foo` no longer hangs on a missing file.** It used to wait for input; it
  now exits with a message naming the path.
```

## Highlights

The GitHub release body is a highlights block plus a link: the `highlights`
fragments, the `upgrade-notes` fragments, and a link to the full changelog
section, which holds every other category. Highlights are written by whoever
cuts the release, not by each pull request: about 3–7 of the changes a user
would most notice, in user language, never an internal refactor. A highlight
restates an entry that keeps its own `added`, `changed` or `fixed` fragment.
[RELEASING.md](../RELEASING.md) has the rule; a cut with no highlights, or more
than seven, is refused.

## Upgrade notes

A change that needs the reader to *do* something (reinstall for a new extra,
re-measure a calibration, rename a setting) also gets an `upgrade-notes`
fragment. The release puts that category right after Highlights, above Added,
and carries it into the GitHub release body in full. One change can have both
an `upgrade-notes` and a `fixed` or `changed` fragment.

## Converting a hand-written entry

A branch that added a bullet under `## [Unreleased]` in `CHANGELOG.md` moves it
here: delete the bullet from `CHANGELOG.md` and save its text, unchanged, as
`changelog.d/<slug>.<category>.md` (the `###` heading it sat under is the
category).
