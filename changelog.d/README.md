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
- **category**: one of `upgrade-notes`, `added`, `changed`, `deprecated`,
  `removed`, `fixed`, `security`.

A malformed name, an unknown category or an empty file fails
`python scripts/bump_version.py --check <version>` and the release tests.

## Body

The entry exactly as it should read in the release notes: one Markdown bullet,
starting with `- `, continuation lines indented two spaces. Lead with a bold
sentence that says what a user sees; the rest explains. A heading line inside
an entry is refused, because it would split the entry out of its category.

```markdown
- **`--foo` no longer hangs on a missing file.** It used to wait for input; it
  now exits with a message naming the path.
```

## Upgrade notes

A change that needs the reader to *do* something (reinstall for a new extra,
re-measure a calibration, rename a setting) also gets an `upgrade-notes`
fragment. The release puts that category first, above Added, so it leads the
GitHub release body. One change can have both an `upgrade-notes` and a `fixed`
or `changed` fragment.

## Converting a hand-written entry

A branch that added a bullet under `## [Unreleased]` in `CHANGELOG.md` moves it
here: delete the bullet from `CHANGELOG.md` and save its text, unchanged, as
`changelog.d/<slug>.<category>.md` (the `###` heading it sat under is the
category).
