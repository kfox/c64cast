# Releasing c64cast

For maintainers. Users want [the README](README.md); contributors want
[CONTRIBUTING.md](CONTRIBUTING.md).

A release is one tag push;
[`.github/workflows/release.yml`](.github/workflows/release.yml) does the rest.

| Artifact | Where it lands |
|---|---|
| `c64cast-X.Y.Z-py3-none-any.whl` | PyPI + the GitHub release |
| `c64cast-X.Y.Z.tar.gz` | PyPI + the GitHub release |
| `c64cast-users-guide-X.Y.Z.pdf` | the GitHub release |
| `c64cast-reference-guide-X.Y.Z.pdf` | the GitHub release |
| `c64cast-performance-card-X.Y.Z.pdf` | the GitHub release |
| The same three PDFs again, unversioned | the GitHub release |
| Release notes | the GitHub release, from that version's `CHANGELOG.md` section |

Each book ships twice because a version-stamped filename is what you want on
disk and an unversioned one is what a link can point at:
`releases/latest/download/c64cast-users-guide.pdf` always serves the current
release, which is how the README links all three. Renaming a book's PDF
therefore breaks a published URL — the filename is part of the interface.

## One-time setup

### PyPI trusted publishing

Uploads use a short-lived OIDC token, so there is no API token to rotate. PyPI
needs telling which workflow to trust. Before the first release the project does
not exist there yet, so this is a *pending* publisher:
<https://pypi.org/manage/account/publishing/>

| Field | Value |
|---|---|
| PyPI Project Name | `c64cast` |
| Owner | `kfox` |
| Repository name | `c64cast` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The environment name must match `environment: name: pypi` in `release.yml`.
After the first publish it becomes a normal trusted publisher under the
project's own *Publishing* settings.

### The `pypi` GitHub environment

Created implicitly by the workflow. Add it under *Settings → Environments* only
if you want a required reviewer on the upload step.

## Cutting a release

**1. Check the changelog.** `## [Unreleased]` becomes the release notes
verbatim, so read it as the announcement it is about to be.

**2. Bump.**

```bash
python scripts/bump_version.py 0.2.0
```

Moves `[project] version`, renames the changelog section and dates it, opens a
fresh `## [Unreleased]`, fixes the link references, and re-runs `uv lock`.

Nothing else needs editing — `__version__`, the `#:schema` URL and every book's
cover or footer version all derive from that number. The exception is the
**edition** line in each bound book's `colophon.md`, which is an editorial call:
see [Editions](#editions).

**3. Open a PR and verify.**

```bash
make check
make books
python scripts/bump_version.py --check 0.2.0
```

`--check` is the same gate the workflow runs against the pushed tag.

For a change to the workflow itself, run it from *Actions → Release → Run
workflow* with **publish** off: full build, checks and book renders, no publish.

**4. Tag.** Once merged, from `main`:

```bash
git switch main && git pull
git tag -a v0.2.0 -m "c64cast v0.2.0"
git push origin v0.2.0
```

That triggers: `--check` against the tag → build → `twine check --strict` →
install the wheel in a clean environment outside the checkout and run it →
render the books → publish to PyPI → create the GitHub release with every
artifact attached and linked from the notes.

PyPI is published before the GitHub release because a PyPI version can only be
yanked, never replaced, while a GitHub release can be recreated.

**5. Check the result.**

```bash
uv tool install 'c64cast[all]'==0.2.0
c64cast --version
```

PyPI renders `README.md`, which cannot be changed without a new release — worth
a look the first time.

## If it goes wrong

**Failed before the publish step.** Nothing was uploaded. Fix the tree, move the
tag (`git push --delete origin v0.2.0`, re-tag, push), or re-run the job.

**PyPI published but the GitHub release failed.** Re-run the workflow if the tag
does not have to move — the upload skips files already on PyPI (`--check-url`).
If the fix needs a new commit, do not re-tag: the tag has to keep pointing at the
commit that built what PyPI already has. Create the release by hand from that
run's artifacts instead (`gh run download <id> -n release-artifacts`):

```bash
gh release create v0.2.0 --title "c64cast v0.2.0" --verify-tag \
  --notes-file <(python scripts/bump_version.py --notes 0.2.0) \
  dist/*.whl dist/*.tar.gz dist/*.pdf
```

**A bad version reached PyPI.** Yank it (*Manage → Releases → Yank*) and release
a patch. Do not delete it — deleting frees the version number for reuse.

## Versioning

Semantic versioning over the user surface: CLI flags, config schema, `example:`
names, data-directory layout. See
[What counts as a breaking change](CONTRIBUTING.md#what-counts-as-a-breaking-change).

## Editions

The version and the edition answer different questions. `VERSION X.Y.Z` on a
book's cover, in its PDF metadata and in the card's header says *which build of
the software this text describes*; it derives from the bump and is never a
judgement call. The **edition** in `colophon.md` says *which book this is*, so
that someone holding a detached PDF can tell whether their copy is the one you
are citing.

Because the cover already carries the release, the edition is free to mean what
it means in print: a new edition is a book you would have to re-read, not a book
with corrections in it. Bump it when the shape or the teaching changes —

- a chapter is added, removed, renumbered or reordered, so the old copy's
  contents no longer line up;
- a chapter is rewritten rather than amended, changing the recommended path
  through the material;
- advice the previous edition gave is now *wrong*, such that following the old
  book gets a bad result rather than an incomplete one.

Flags and config fields documented in place, corrections, new sections within a
chapter and typography work are all the same edition, as are the reference
guide's appendices and index — those regenerate on every build by construction.

The month is the date the edition was established, not the date the PDF was
rendered, so it moves only when the ordinal does: a colophon reading `1st
Edition, July 2026` under a cover reading `VERSION 0.4.0` is correct, not stale.
Both bound books advance together when either earns it.
