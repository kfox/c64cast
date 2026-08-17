"""Guards for the release machinery: scripts/bump_version.py and release.yml."""

from __future__ import annotations

import contextlib
import importlib
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
    """Import scripts/bump_version.py by path; `scripts/` is not a package."""
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


def _book_outputs() -> list[str]:
    """Every book's artifact basename, from the books themselves."""
    docs = os.path.join(_REPO, "docs")
    names = []
    for entry in sorted(os.listdir(docs)):
        path = os.path.join(docs, entry, "book.toml")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                names.append(tomllib.load(f)["book"]["output"])
    assert names, "no docs/*/book.toml — did the books move?"
    return names


# Shaped like the real changelog: the preamble names the Unreleased heading
# inline, above the heading itself.
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
        self.assertNotIn("[Unreleased]: ", body)

    def test_unreleased_section_still_exists(self) -> None:
        self.assertRegex(_read("CHANGELOG.md"), r"(?m)^## \[Unreleased\][ \t]*$")


class TestUpgradeNotesConvention(unittest.TestCase):
    """`### Upgrade notes` leads a version's section, or is absent entirely.

    A version's section becomes its GitHub release body verbatim, so the block
    only does its job -- being read before anyone downloads anything -- while it
    sits at the top. One spelling, because a reader who learns to look for it in
    one release has to find it in the next.
    """

    HEADING = "### Upgrade notes"

    def test_the_preamble_documents_the_convention(self) -> None:
        # Split on the first heading at line start: the preamble names
        # `## [Unreleased]` inline, above the heading itself.
        preamble = re.split(r"(?m)^## \[", _read("CHANGELOG.md"), maxsplit=1)[0]
        self.assertIn(
            "Upgrade notes",
            preamble,
            "the changelog no longer explains its own Upgrade notes convention",
        )

    def test_it_is_spelled_one_way(self) -> None:
        strays = [
            line
            for line in _read("CHANGELOG.md").splitlines()
            if re.match(r"^#+\s+upgrad", line, re.I) and line != self.HEADING
        ]
        self.assertEqual(strays, [], f"spell the heading exactly {self.HEADING!r}")

    def test_it_leads_the_section_it_appears_in(self) -> None:
        changelog = _read("CHANGELOG.md")
        for version, _ in bv.sections(changelog):
            subsections = re.findall(r"(?m)^### .+$", bv.section_body(changelog, version))
            if self.HEADING not in subsections:
                continue
            with self.subTest(version=version):
                self.assertEqual(
                    subsections[0],
                    self.HEADING,
                    f"[{version}] buries its upgrade notes under "
                    f"{subsections[0]!r} -- they have to lead the release body",
                )


class TestBumpRewrites(unittest.TestCase):
    def test_pyproject_version_is_replaced(self) -> None:
        before = _read("pyproject.toml")
        after = bv.apply_pyproject(before, "9.9.9")
        self.assertEqual(tomllib.loads(after)["project"]["version"], "9.9.9")
        self.assertEqual(
            tomllib.loads(after)["tool"]["ruff"]["target-version"],
            tomllib.loads(before)["tool"]["ruff"]["target-version"],
        )

    def test_cut_renames_the_heading_not_the_preamble(self) -> None:
        out = bv.apply_changelog(_CHANGELOG, "1.0.0", "2026-07-29")
        self.assertIn("Work lands under `## [Unreleased]`;", out)
        self.assertEqual(
            bv.sections(out),
            [("Unreleased", None), ("1.0.0", "2026-07-29")],
        )

    def test_cut_keeps_a_blank_line_after_the_new_heading(self) -> None:
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
        self.assertEqual(second.count("[Unreleased]: "), 1)
        self.assertIn("compare/v1.1.0...HEAD", second)
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
        # Comment lines dropped: a comment may name a tool the steps must not
        # use, which raw-text matching cannot distinguish from using it.
        cls.code = "\n".join(
            line for line in cls.yaml.splitlines() if not line.lstrip().startswith("#")
        )

    def test_it_triggers_on_version_tags(self) -> None:
        self.assertIn('tags: ["v*"]', self.code)

    def test_it_calls_the_bump_script_flags_that_exist(self) -> None:
        self.assertIn("scripts/bump_version.py", self.code)
        for flag in ("--check", "--notes"):
            self.assertIn(flag, self.code, f"the workflow no longer passes {flag}")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                bv.main([flag, "--definitely-not-a-flag", "1.2.3"])
            self.assertNotIn(f"unrecognized arguments: {flag}", err.getvalue())

    def _smoke_test_imports(self) -> list[tuple[str, str, object]]:
        """Every `from c64cast... import X` in the workflow, resolved."""
        pairs = re.findall(r"(?m)^\s*from (c64cast[\w.]*) import (\w+)$", self.code)
        self.assertTrue(pairs, "the wheel smoke test no longer imports c64cast")
        resolved: list[tuple[str, str, object]] = []
        for package, name in pairs:
            try:
                # A submodule is not an attribute of its package until imported,
                # so the plain attribute lookup has to come second.
                member: object = importlib.import_module(f"{package}.{name}")
            except ImportError:
                member = getattr(importlib.import_module(package), name, None)
            self.assertIsNotNone(
                member,
                f"release.yml imports {name} from {package}, which has no such member",
            )
            resolved.append((package, name, member))
        return resolved

    def test_the_smoke_test_imports_modules_that_exist(self) -> None:
        # The smoke test runs against an installed wheel from outside the
        # checkout, so nothing but a release exercises these imports -- a module
        # that moves in a refactor fails at the tag, after the merge.
        self._smoke_test_imports()

    def test_the_smoke_test_calls_functions_that_exist(self) -> None:
        for _, name, member in self._smoke_test_imports():
            for call in re.findall(rf"(?m)^\s*\w+ = {name}\.(\w+)\(", self.code):
                with self.subTest(f"{name}.{call}()"):
                    self.assertTrue(
                        callable(getattr(member, call, None)),
                        f"release.yml calls {name}.{call}(), which is gone",
                    )

    def test_it_renders_the_books_through_the_make_target(self) -> None:
        self.assertIn("make books", self.code)
        makefile = _read("Makefile")
        self.assertRegex(makefile, r"(?m)^books:", "the `books` Make target is gone")

    def test_publishing_happens_before_the_github_release(self) -> None:
        self.assertIn("needs: [build, publish-pypi]", self.code)

    def test_pypi_upload_uses_trusted_publishing(self) -> None:
        self.assertIn("id-token: write", self.code)
        self.assertIn("name: pypi", self.code)
        self.assertIn("--trusted-publishing always", self.code)

    def test_the_upload_is_digest_pinnable(self) -> None:
        # A Docker action resolving its image by action ref cannot be pinned.
        self.assertNotIn("gh-action-pypi-publish", self.code)
        self.assertIn("uv publish", self.code)

    def test_the_release_body_links_every_book_and_the_package(self) -> None:
        self.assertIn("releases/download/v$VERSION", self.code)
        # Rendering and uploading are wildcarded over docs/*/, so a book that
        # nobody linked would ship as an asset nobody can find. The notes are
        # hand-written, so this is the one place a new book has to be named.
        for output in _book_outputs():
            self.assertIn(f"{output}-$VERSION.pdf", self.code, f"{output} is not linked")
        # Versioned filenames, so a "latest" download URL cannot serve them.
        self.assertNotIn("releases/latest/download", self.code)
        self.assertIn("--notes-file body.md", self.code)

    def test_the_body_leads_with_how_to_install_and_upgrade(self) -> None:
        # A page that opens with a list of files teaches that upgrading means
        # downloading files, which is the one thing that cannot upgrade an
        # install. The order is the point, so it is the thing asserted.
        for needle in ("### Install or upgrade", "uv tool upgrade c64cast"):
            self.assertIn(needle, self.code, f"the release body no longer says {needle!r}")
        self.assertLess(
            self.code.index("### Install or upgrade"),
            self.code.index("### Distributions"),
            "the distributions are listed above the commands that fetch them",
        )

    def test_the_body_links_a_guide_section_that_exists(self) -> None:
        # The notes point at the User's Guide by anchor, which github.com
        # resolves silently to the top of the page when it is wrong.
        self.assertIn("docs/guide/04-setting-up.md#upgrading", self.code)
        self.assertIn(
            "\n## Upgrading\n",
            _read(os.path.join("docs", "guide", "04-setting-up.md")),
            "the release body links a guide section that is gone",
        )

    def test_every_action_is_pinned_to_a_digest(self) -> None:
        """Across every workflow, not only this one.

        A tag is mutable, so an unpinned action is whatever its owner pushed
        last. The rule is the repository's rather than the release's; it lives
        here because release.yml is where it first mattered.
        """
        workflows = os.path.join(_REPO, ".github", "workflows")
        for name in sorted(os.listdir(workflows)):
            if not name.endswith((".yml", ".yaml")):
                continue
            for ref in re.findall(r"^\s*uses: (\S+)", _read(f".github/workflows/{name}"), re.M):
                self.assertRegex(
                    ref,
                    r"@[0-9a-f]{40}$",
                    f"{name}: {ref} is not pinned to a full commit SHA",
                )

    def test_every_book_asset_carries_the_version(self) -> None:
        for output in _book_outputs():
            self.assertIn(f"{output}-", self.code)

    def test_every_book_also_ships_unversioned(self) -> None:
        # The README links each book as
        # releases/latest/download/<output>.pdf, which only resolves while an
        # asset is named exactly that. Drop the second copy and three published
        # links 404 at the next release, silently.
        self.assertIn('cp "$pdf" "dist/$name.pdf"', self.code)
        readme = _read("README.md")
        for output in _book_outputs():
            self.assertIn(
                f"releases/latest/download/{output}.pdf",
                readme,
                f"{output} has no evergreen link in the README",
            )


if __name__ == "__main__":
    unittest.main()
