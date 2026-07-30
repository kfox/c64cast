"""Guards for the release machinery: scripts/bump_version.py and release.yml.

A release is the one operation this project cannot take back — a version on
PyPI can be yanked but never replaced — and it runs rarely enough that nobody
has the sequence in their head. So the parts that can silently rot are
asserted here rather than discovered mid-release:

  1. The version is declared in exactly one place, and `uv.lock`'s record of
     the project agrees with it. A disagreement makes CI's `uv sync --frozen`
     fail with "lockfile is out of date", which reads like a dependency problem
     and is really a half-finished release.
  2. `bump_version.py`'s rewrites are string surgery on two files whose shape
     it does not control. The Unreleased-heading case has already bitten once:
     the changelog's own preamble mentions `## [Unreleased]` in backticks
     several paragraphs above the real heading, so a substring replace rewrites
     the sentence and leaves the heading alone — producing a file with no
     release section and no error.
  3. The changelog section for the current version is what becomes the GitHub
     release notes, so it has to exist and be extractable.
  4. release.yml's steps refer to script flags and Make targets by name across
     a YAML/shell boundary that no type checker sees.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tomllib
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")


def _load_bump_version():
    """Import scripts/bump_version.py, which is a script rather than a module.

    `scripts/` is not a package and is deliberately not importable from the
    installed wheel, so this goes through the file path instead of sys.path.
    """
    path = os.path.join(_SCRIPTS, "bump_version.py")
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bump_version"] = module
    spec.loader.exec_module(module)
    return module


bv = _load_bump_version()


def _read(name: str) -> str:
    with open(os.path.join(_REPO, name), encoding="utf-8") as f:
        return f.read()


# A changelog shaped like the real one, including the trap: the preamble
# mentions the Unreleased heading inline, before the heading itself.
_CHANGELOG = """\
# Changelog

Work lands under `## [Unreleased]`; cutting a release renames that section to
the version and stamps it with the date.

## [Unreleased]

### Added

- A thing worth announcing.

[Unreleased]: https://github.com/kfox/c64cast/commits/main
"""


class TestVersionIsSingleSourced(unittest.TestCase):
    def test_pyproject_declares_a_release_version(self) -> None:
        version = bv.pyproject_version()
        self.assertRegex(
            version,
            bv.VERSION_RE,
            f"pyproject.toml's version {version!r} is not a releasable version",
        )

    def test_uv_lock_agrees_with_pyproject(self) -> None:
        self.assertEqual(
            bv.lock_version(),
            bv.pyproject_version(),
            "uv.lock's c64cast entry disagrees with pyproject.toml — run `uv lock`",
        )

    def test_only_one_top_level_version_key_in_pyproject(self) -> None:
        # apply_pyproject rewrites `^version = "..."` and asserts a single
        # match. If a future key collides, that assert fires during a release;
        # this fires during a normal test run instead.
        matches = bv.PYPROJECT_VERSION_RE.findall(_read("pyproject.toml"))
        self.assertEqual(
            matches,
            [bv.pyproject_version()],
            "more than one line matches the version regex in pyproject.toml",
        )


class TestChangelogIsReleasable(unittest.TestCase):
    def test_current_version_has_a_dated_section(self) -> None:
        version = bv.pyproject_version()
        problems = [p for p in bv.check(version) if "CHANGELOG" in p]
        self.assertEqual(
            problems,
            [],
            "the changelog is not ready to release the declared version",
        )

    def test_notes_can_be_extracted_for_the_current_version(self) -> None:
        body = bv.section_body(_read("CHANGELOG.md"), bv.pyproject_version())
        self.assertTrue(body.strip(), "the release notes for this version are empty")
        # Link-reference definitions are document-level, not section content;
        # they would render as stray text at the top of a release page.
        self.assertNotIn("[Unreleased]: ", body)

    def test_unreleased_section_still_exists(self) -> None:
        # Work has to have somewhere to land after a release. A cut that
        # forgets to reopen the section sends the next entry into the released
        # one, silently rewriting history that is already published.
        self.assertRegex(_read("CHANGELOG.md"), r"(?m)^## \[Unreleased\][ \t]*$")


class TestBumpRewrites(unittest.TestCase):
    def test_pyproject_version_is_replaced(self) -> None:
        before = _read("pyproject.toml")
        after = bv.apply_pyproject(before, "9.9.9")
        self.assertEqual(tomllib.loads(after)["project"]["version"], "9.9.9")
        # Nothing else moved: the other version-ish keys must be untouched.
        self.assertEqual(
            tomllib.loads(after)["tool"]["ruff"]["target-version"],
            tomllib.loads(before)["tool"]["ruff"]["target-version"],
        )

    def test_cut_renames_the_heading_not_the_preamble(self) -> None:
        out = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        # The prose mention survives verbatim...
        self.assertIn("Work lands under `## [Unreleased]`;", out)
        # ...and the real heading became the release, with a fresh one above it.
        self.assertEqual(
            bv.sections(out),
            [("Unreleased", None), ("1.0.0", "2026-07-29")],
        )

    def test_cut_keeps_a_blank_line_after_the_new_heading(self) -> None:
        # `\\s*$` in the heading regex would eat it, gluing the heading to its
        # first paragraph.
        out = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        self.assertIn("## [1.0.0] - 2026-07-29\n\n### Added", out)

    def test_cut_moves_the_release_body_into_the_new_section(self) -> None:
        out = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        self.assertIn("A thing worth announcing.", bv.section_body(out, "1.0.0"))
        self.assertEqual(bv.section_body(out, "Unreleased").strip(), "Nothing yet.")

    def test_first_release_links_to_its_own_tag(self) -> None:
        out = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        self.assertIn(
            "[1.0.0]: https://github.com/kfox/c64cast/releases/tag/v1.0.0",
            out,
        )
        self.assertIn(
            "[Unreleased]: https://github.com/kfox/c64cast/compare/v1.0.0...HEAD",
            out,
        )
        self.assertNotIn("/commits/main", out)

    def test_second_release_links_to_a_diff_from_the_previous(self) -> None:
        first = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        second = bv.apply_changelog(first, "1.1.0", "2026-09-01")
        self.assertIn(
            "[1.1.0]: https://github.com/kfox/c64cast/compare/v1.0.0...v1.1.0",
            second,
        )
        # Exactly one Unreleased ref, pointing at the newest tag.
        self.assertEqual(second.count("[Unreleased]: "), 1)
        self.assertIn("compare/v1.1.0...HEAD", second)
        # The older sections and their refs are left alone.
        self.assertEqual(
            bv.sections(second),
            [("Unreleased", None), ("1.1.0", "2026-09-01"), ("1.0.0", "2026-07-29")],
        )

    def test_cutting_the_same_version_twice_is_refused(self) -> None:
        once = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        with self.assertRaises(bv.BumpError):
            bv.apply_changelog(once, "1.0.0", "2026-07-30")

    def test_cut_without_an_unreleased_section_is_refused(self) -> None:
        with self.assertRaises(bv.BumpError):
            bv.apply_changelog("# Changelog\n\n## [1.0.0] - 2026-01-01\n", "1.1.0", "2026-07-29")


class TestVersionArgumentParsing(unittest.TestCase):
    def test_a_leading_v_is_accepted(self) -> None:
        # The tag is `v1.2.3` and the version is `1.2.3`; typing either should
        # work, since the workflow and a human reach for different ones.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = bv.main(["--notes", "v" + bv.pyproject_version()])
        self.assertEqual(status, 0)
        self.assertTrue(out.getvalue().strip(), "--notes printed nothing")

    def test_prereleases_are_valid_versions(self) -> None:
        for version in ("1.2.3", "1.2.3rc1", "1.2.3a1", "1.2.3b2", "1.2.3.dev4"):
            self.assertRegex(version, bv.VERSION_RE, f"{version} should be releasable")

    def test_junk_versions_are_rejected(self) -> None:
        for version in ("1.2", "1.2.3.4", "latest", "1.2.3+local", "v1.2.3rc"):
            self.assertNotRegex(version, bv.VERSION_RE, f"{version} should not be releasable")

    def test_a_junk_version_exits_two(self) -> None:
        # Exit 2 is the usage-error convention used across this project's CLI.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = bv.main(["not-a-version"])
        self.assertEqual(status, 2)
        self.assertIn("not a release version", err.getvalue())


class TestReleaseWorkflow(unittest.TestCase):
    """The workflow calls into this repo across a YAML/shell boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.yaml = _read(os.path.join(".github", "workflows", "release.yml"))

    def test_it_triggers_on_version_tags(self) -> None:
        self.assertIn('tags: ["v*"]', self.yaml)

    def test_it_calls_the_bump_script_flags_that_exist(self) -> None:
        # Both are parsed by argparse, so a renamed flag would fail at release
        # time. Matched loosely (the invocations are line-continued shell), then
        # checked against argparse itself rather than against a second literal.
        self.assertIn("scripts/bump_version.py", self.yaml)
        for flag in ("--check", "--notes"):
            self.assertIn(flag, self.yaml, f"the workflow no longer passes {flag}")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                # An unknown flag is a parser error; a known one gets past it.
                bv.main([flag, "--definitely-not-a-flag", "1.2.3"])
            self.assertNotIn(f"unrecognized arguments: {flag}", err.getvalue())

    def test_it_renders_the_guide_through_the_make_target(self) -> None:
        self.assertIn("make guide", self.yaml)
        makefile = _read("Makefile")
        self.assertRegex(makefile, r"(?m)^guide:", "the `guide` Make target is gone")

    def test_publishing_happens_before_the_github_release(self) -> None:
        # The ordering that makes a failed upload recoverable.
        self.assertIn("needs: [build, publish-pypi]", self.yaml)

    def test_pypi_upload_uses_trusted_publishing(self) -> None:
        # No API token in this repo: the upload is OIDC, which needs
        # id-token: write and an environment PyPI can be told to trust.
        self.assertIn("id-token: write", self.yaml)
        self.assertIn("name: pypi", self.yaml)

    def test_every_action_is_pinned_to_a_digest(self) -> None:
        # Same rule as ci.yml: a tag is mutable, a digest is not.
        for ref in re.findall(r"^\s*uses: (\S+)", self.yaml, re.M):
            self.assertRegex(
                ref,
                r"@[0-9a-f]{40}$",
                f"{ref} is not pinned to a full commit SHA",
            )

    def test_the_guide_asset_carries_the_version(self) -> None:
        self.assertIn("c64cast-users-guide-", self.yaml)


if __name__ == "__main__":
    unittest.main()
