# Releasing c64cast

For maintainers. Users want [the README](README.md); contributors want
[CONTRIBUTING.md](CONTRIBUTING.md).

A release is one tag push. Everything else — building, checking, publishing to
PyPI, rendering the User's Guide, creating the GitHub release — is
[`.github/workflows/release.yml`](.github/workflows/release.yml).

## What a release consists of

| Artifact | Where it lands |
|---|---|
| `c64cast-X.Y.Z-py3-none-any.whl` | PyPI + the GitHub release |
| `c64cast-X.Y.Z.tar.gz` (sdist) | PyPI + the GitHub release |
| `c64cast-users-guide-X.Y.Z.pdf` | the GitHub release |
| Release notes | the GitHub release, from that version's `CHANGELOG.md` section |

The wheel is `py3-none-any` — pure Python, no compiled extensions — so one
artifact serves macOS, Linux and Windows. What backs those platform claims is
the `ubuntu × macos × windows` test matrix in `ci.yml`, not per-platform
builds.

## One-time setup

Both of these are done once per repository, by hand, and neither can be done
from a workflow.

### 1. PyPI trusted publishing

The workflow uploads with a short-lived OIDC token rather than an API token, so
there is no secret in this repo to leak or rotate. PyPI has to be told which
workflow to trust.

For the **first** release the project does not exist on PyPI yet, so this is a
*pending* publisher: <https://pypi.org/manage/account/publishing/>

| Field | Value |
|---|---|
| PyPI Project Name | `c64cast` |
| Owner | `kfox` |
| Repository name | `c64cast` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

After the first publish it becomes a normal trusted publisher under the
project's own *Publishing* settings, and needs no further attention.

> [!IMPORTANT]
> The environment name must match the `environment: name: pypi` in
> `release.yml`. A mismatch fails at upload with an OIDC error, after the
> version has already been built — annoying but harmless, since nothing is
> uploaded.

### 2. The `pypi` GitHub environment

Referencing it in the workflow creates it implicitly, so this is optional. Add
it under *Settings → Environments* if you want a required reviewer on the
upload step — the last point at which a release can be stopped.

## Cutting a release

### 1. Land everything, and check the changelog

Anything a user would notice should already have an entry under
`## [Unreleased]`. That section becomes the release notes verbatim, so read it
as the announcement it is about to be, not as a list of commits.

### 2. Bump

```bash
python scripts/bump_version.py 0.2.0
```

This moves `[project] version` in `pyproject.toml`, renames the changelog's
`## [Unreleased]` section to `## [0.2.0] - <today>`, opens a fresh empty one,
fixes up the link references at the foot of the file, and re-runs `uv lock` so
the lockfile's own `c64cast` entry matches (CI runs `uv sync --frozen`, which
fails on a stale lockfile).

Nothing else needs editing. `__version__`, the `#:schema` URL a generated
config points at, and the User's Guide cover all derive from that one number.

The guide's **edition line** is the exception, because it is editorial rather
than mechanical: `docs/guide/colophon.md` says "1st Edition, July 2026". A
release with substantially rewritten chapters is a new edition; a patch release
is not.

### 3. Open a PR, verify, merge

```bash
make check                       # ruff + mypy --strict + pyright + the suite
make guide                       # the PDF still renders, with the new version on the cover
python scripts/bump_version.py --check 0.2.0
```

`--check` is the same gate the release workflow runs against the pushed tag, so
a green `--check` here means the tag will not be rejected there.

To exercise the whole workflow without spending a version number, run it from
*Actions → Release → Run workflow* with **publish** left off. That builds,
verifies, smoke-tests the wheel in a clean environment and renders the guide,
then stops before both publish steps.

### 4. Tag

Once the PR is merged, from `main`:

```bash
git switch main && git pull
git tag -a v0.2.0 -m "c64cast v0.2.0"
git push origin v0.2.0
```

The tag push is the trigger. The workflow then:

1. resolves the version from the tag and re-runs `--check` against the tree, so
   a tag that disagrees with `pyproject.toml` stops here;
2. builds the sdist and wheel, and runs `twine check --strict`;
3. installs the built wheel into a clean environment **outside the checkout**
   and runs it — `--version`, `--list-examples`, `--print-example`, and a probe
   that the packaged schema and example configs are really in the wheel;
4. renders the User's Guide PDF and names it for the release;
5. publishes to PyPI (with a PEP 740 provenance attestation);
6. creates the GitHub release with the wheel, the sdist and the PDF attached.

PyPI comes before the GitHub release deliberately: a PyPI version can never be
replaced, only yanked, whereas a GitHub release can be deleted and recreated.
If the upload fails, no release page ends up pointing at a package that is not
there, and re-running the workflow after a fix is safe.

### 5. Check the result

```bash
uv tool install 'c64cast[all]'==0.2.0
c64cast --version
c64cast --doctor --skip-probe
```

The PyPI badge in the README goes live with the first publish, and the project
page renders `README.md` — worth a look the first time, since a PyPI page
cannot be edited without a new release.

## If it goes wrong

**The workflow failed before the publish step.** Nothing happened. Fix the
tree, delete and re-push the tag (`git tag -d v0.2.0 && git push --delete
origin v0.2.0`, then re-tag), or re-run the job from the Actions tab.

**PyPI published but the GitHub release failed.** The version is out. Do not
try to re-publish — re-run the workflow; the publish step will fail on "file
already exists" if reached, so create the release by hand instead:

```bash
gh release create v0.2.0 --title "c64cast v0.2.0" \
  --notes-file <(python scripts/bump_version.py --notes 0.2.0) \
  dist/*.whl dist/*.tar.gz dist/c64cast-users-guide-0.2.0.pdf
```

**A bad version reached PyPI.** It cannot be replaced. Yank it
(*Manage → Releases → Yank*), which hides it from new installs while leaving it
resolvable for anyone who already pinned it, then release a patch. Do not
delete the release — that frees the version number for reuse and breaks the
one-version-one-artifact assumption every lockfile depends on.

## Versioning

Semantic versioning over the *user* surface: the CLI flags, the config schema,
the `example:` names, and the data-directory layout. The Python API carries no
stability promise while the version is `0.x`. See
[What counts as a breaking change](CONTRIBUTING.md#what-counts-as-a-breaking-change).
