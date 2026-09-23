"""Tests for the suite's own filesystem sandbox (tests/_fs_sandbox.py).

Two things are being checked, and they fail for different reasons. The rule
itself — which paths are in bounds — is exercised through `violation()`, which
is pure. Whether the rule is actually *in force* depends on every entry point
still putting `tests` on `PYTHONPATH` so `sitecustomize` runs, and that is a
four-file agreement nothing else would notice breaking: a run with the sandbox
silently disarmed looks exactly like a run with nothing to report.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _fs_sandbox

from c64cast.app import paths
from c64cast.hw import char_rom

CHECKOUT = Path(_fs_sandbox.CHECKOUT)

# Every place the suite is started from. Each has to set PYTHONPATH itself:
# the environment is what reaches unittest_parallel's worker processes.
ENTRY_POINTS = (
    "Makefile",
    "scripts/pre-commit.sh",
    "scripts/coverage.sh",
    ".github/workflows/ci.yml",
)


class RuleTest(unittest.TestCase):
    """`violation()` is pure, so the rule can be checked without touching a
    file or arming anything."""

    def test_the_checkout_is_in_bounds(self):
        self.assertIsNone(_fs_sandbox.violation(str(CHECKOUT / "c64cast" / "app" / "cli.py")))

    def test_a_temp_dir_is_in_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_fs_sandbox.violation(os.path.join(tmp, "fixture.toml")))

    def test_the_interpreter_is_in_bounds(self):
        # uv and mise both keep interpreters under ~/.local/share, so the
        # stdlib itself sits inside the region the rule otherwise denies.
        self.assertIsNone(_fs_sandbox.violation(os.__file__))

    def test_a_path_outside_home_is_not_policed(self):
        self.assertIsNone(_fs_sandbox.violation("/etc/localtime"))

    def test_the_real_machine_settings_are_out_of_bounds(self):
        complaint = _fs_sandbox.violation(
            str(Path.home() / ".config" / "c64cast" / "settings.toml")
        )
        assert complaint is not None
        self.assertIn("outside the checkout", complaint)

    def test_the_real_data_dir_is_out_of_bounds(self):
        complaint = _fs_sandbox.violation(
            str(Path.home() / ".local" / "share" / "c64cast" / "roms" / "chargen.bin")
        )
        assert complaint is not None
        self.assertIn("outside the checkout", complaint)

    def test_a_sibling_of_an_allowed_root_does_not_borrow_its_permission(self):
        # Prefix matching without a trailing separator would let a directory
        # whose name merely starts the same way pass.
        #
        # Only observable where the checkout sits inside the policed region.
        # The rule deliberately allows everything outside $HOME without
        # enumerating it, so where the checkout is on another volume entirely
        # — Windows CI puts it on D: while $HOME is on C: — the sibling is
        # allowed on that ground alone and proves nothing about prefixes.
        if not _fs_sandbox._key(_fs_sandbox.CHECKOUT).startswith(_fs_sandbox._HOME):
            self.skipTest("checkout is outside $HOME, where nothing is policed")
        self.assertIsNotNone(_fs_sandbox.violation(str(CHECKOUT) + "-scratch/notes.txt"))

    def test_a_gitignored_asset_is_out_of_bounds(self):
        complaint = _fs_sandbox.violation(str(CHECKOUT / "assets" / "roms" / "chargen.bin"))
        assert complaint is not None
        self.assertIn("gitignored", complaint)

    def test_a_tracked_asset_is_in_bounds(self):
        for rel in ("assets/logo.png", "assets/roms/README.md"):
            with self.subTest(rel=rel):
                self.assertIsNone(_fs_sandbox.violation(str(CHECKOUT / rel)))

    def test_the_checkouts_own_git_is_out_of_bounds(self):
        """The one leak the outside-the-checkout rule could never catch, since
        `.git` is *inside* the tree the suite is otherwise free to write. A
        fixture that reached `.git/config` left `user.name = Test` there and
        misattributed 17 commits."""
        for rel in (".git/config", ".git/HEAD", ".git/hooks/pre-commit"):
            with self.subTest(rel=rel):
                complaint = _fs_sandbox.violation(str(CHECKOUT / rel))
                assert complaint is not None
                self.assertIn(".git", complaint)

    def test_a_sibling_of_git_is_not_caught_by_it(self):
        """`.gitignore` and `.github/` are ordinary tracked files, which is why
        the key carries a trailing separator."""
        for rel in (".gitignore", ".github/workflows/ci.yml", ".git-blame-ignore-revs"):
            with self.subTest(rel=rel):
                self.assertIsNone(_fs_sandbox.violation(str(CHECKOUT / rel)))

    def test_every_git_dir_this_checkout_has_is_covered(self):
        """In a worktree the metadata is not under the tree at all: `.git` is a
        file naming a directory in the *primary* checkout, and `config` — the
        file #482 corrupted — lives in the common dir beside it. Every change in
        this repository is made in a worktree, so a rule that covered only the
        local name would be unguarded exactly where the work happens."""
        for key in _fs_sandbox._GIT:
            with self.subTest(key=key):
                complaint = _fs_sandbox.violation(os.path.join(key, "config"))
                self.assertIsNotNone(complaint)
        self.assertIsNone(_fs_sandbox.violation(str(CHECKOUT / "CHANGELOG.md")))


class GitEnvConflictTest(unittest.TestCase):
    """`GIT_DIR` outranks `-C`, so a fixture that looks self-contained is not.

    The rule is *disagreement*, not presence: under the pre-commit hook every
    subprocess inherits a `GIT_DIR` naming the checkout, and the calls that
    genuinely mean the checkout have to keep working.
    """

    def test_a_git_dir_pointing_away_from_the_dash_c_target_is_refused(self):
        complaint = _fs_sandbox.git_env_conflict(
            ["git", "-C", "/tmp/scratch", "config", "user.name", "Test"],
            cwd=str(CHECKOUT),
            env={"GIT_DIR": str(CHECKOUT / ".git")},
        )
        assert complaint is not None
        self.assertIn("outranks -C", complaint)

    def test_the_index_file_and_work_tree_are_the_same_trap(self):
        for name in ("GIT_WORK_TREE", "GIT_INDEX_FILE"):
            with self.subTest(name=name):
                complaint = _fs_sandbox.git_env_conflict(
                    ["git", "-C", "/tmp/scratch", "add", "m.py"],
                    cwd=str(CHECKOUT),
                    env={name: str(CHECKOUT)},
                )
                self.assertIsNotNone(complaint)

    def test_a_git_dir_inside_the_dash_c_target_is_what_the_caller_asked_for(self):
        """`git -C <checkout> ls-files` under the pre-commit hook: the two agree,
        so there is nothing ambiguous to refuse."""
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "-C", str(CHECKOUT), "ls-files", "assets"],
                cwd=str(CHECKOUT),
                env={"GIT_DIR": str(CHECKOUT / ".git")},
            )
        )

    def test_no_dash_c_means_the_ambient_repository_was_meant(self):
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "config", "user.name", "Test"],
                cwd="/tmp/scratch",
                env={"GIT_DIR": str(CHECKOUT / ".git")},
            )
        )

    def test_a_clean_environment_is_never_a_conflict(self):
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "-C", "/tmp/scratch", "init", "-q"], cwd=str(CHECKOUT), env={}
            )
        )

    def test_chained_dash_c_options_resolve_relative_to_each_other(self):
        """git applies each `-C` from where the last one left it, so a rule that
        read only the final one would resolve `sub` against cwd."""
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "-C", str(CHECKOUT), "-C", ".git", "rev-parse", "HEAD"],
                cwd="/tmp/elsewhere",
                env={"GIT_DIR": str(CHECKOUT / ".git")},
            )
        )

    def test_a_worktrees_own_git_dir_is_not_a_conflict_with_that_worktree(self):
        """The case that made the gate refuse its own commit.

        A worktree's metadata lives under the *primary* checkout's `.git`, so
        `GIT_DIR` is never inside the tree it belongs to. Asking only whether
        it is read the pre-commit hook's own environment as a conflict with the
        very worktree it was exported for, and every `git -C <worktree>` in the
        suite failed — under the hook, where nothing else runs.
        """
        root = Path(tempfile.mkdtemp())
        tree, gitdir = root / "tree", root / "primary" / ".git" / "worktrees" / "tree"
        gitdir.mkdir(parents=True)
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")

        for name, value in (
            ("GIT_DIR", gitdir),
            ("GIT_INDEX_FILE", gitdir / "index"),
        ):
            with self.subTest(name=name, value=str(value)):
                self.assertIsNone(
                    _fs_sandbox.git_env_conflict(
                        ["git", "-C", str(tree), "config", "user.name"],
                        cwd=str(root),
                        env={name: str(value)},
                    )
                )

    def test_a_foreign_worktree_is_still_a_conflict(self):
        """The near miss: same shape, but `GIT_DIR` names a *different*
        worktree's metadata. Nothing about the layout makes that agree."""
        root = Path(tempfile.mkdtemp())
        tree, mine = root / "tree", root / "primary" / ".git" / "worktrees" / "tree"
        mine.mkdir(parents=True)
        theirs = root / "other" / ".git"
        theirs.mkdir(parents=True)
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {mine}\n", encoding="utf-8")

        complaint = _fs_sandbox.git_env_conflict(
            ["git", "-C", str(tree), "config", "user.name", "Test"],
            cwd=str(root),
            env={"GIT_DIR": str(theirs)},
        )
        assert complaint is not None
        self.assertIn("outranks -C", complaint)

    def test_a_global_option_with_a_separate_value_does_not_hide_the_dash_c(self):
        """`git -c core.quotePath=false -C <tmp> …` is an idiom this repository
        already writes. Ending the option walk at the first argument that does
        not start with `-` reports no `-C` at all, which allows exactly the
        call the guard exists to refuse."""
        for lead in (
            ["-c", "core.quotePath=false"],
            ["--exec-path", "/opt/libexec/git-core"],
            ["--namespace", "refs/test"],
            ["--no-pager", "-c", "core.quotePath=false"],
        ):
            with self.subTest(lead=" ".join(lead)):
                complaint = _fs_sandbox.git_env_conflict(
                    ["git", *lead, "-C", "/tmp/scratch", "config", "user.name", "Test"],
                    cwd=str(CHECKOUT),
                    env={"GIT_DIR": str(CHECKOUT / ".git")},
                )
                assert complaint is not None
                self.assertIn("outranks -C", complaint)

    def test_the_other_separate_value_options_do_not_hide_it_either(self):
        """`-c` was not the only one: `--attr-source <tree>` and
        `--shallow-file <path>` take a separate value too, and while the table
        listed neither, the walk ended on the value and reported no `-C`.

        The complaint has to be the one that names the target: an option the
        tables do not carry is refused as well, but for not being readable,
        and that tells the reader to fix a table rather than the fixture.
        """
        for lead in (["--attr-source", "HEAD"], ["--shallow-file", "/tmp/shallow"]):
            with self.subTest(lead=" ".join(lead)):
                complaint = _fs_sandbox.git_env_conflict(
                    ["git", *lead, "-C", "/tmp/scratch", "config", "user.name", "Test"],
                    cwd=str(CHECKOUT),
                    env={"GIT_DIR": str(CHECKOUT / ".git")},
                )
                assert complaint is not None
                self.assertIn("outranks -C", complaint)
                self.assertIn("/tmp/scratch", complaint)

    def test_an_option_in_neither_table_is_refused_rather_than_skipped(self):
        """Which is the same hole once more for every name not listed yet.
        Skipping one that takes a separate value leaves the walk on the value,
        so the guard reports no `-C` and allows the write. An option it cannot
        classify now names itself in a failure instead."""
        complaint = _fs_sandbox.git_env_conflict(
            ["git", "--not-yet", "x", "-C", "/tmp/scratch", "config", "user.name", "Test"],
            cwd=str(CHECKOUT),
            env={"GIT_DIR": str(CHECKOUT / ".git")},
        )
        assert complaint is not None
        self.assertIn("--not-yet", complaint)

    def test_an_unreadable_option_with_nothing_ambient_is_not_a_conflict(self):
        """`make test` exports no `GIT_*`, so there is no repository for the
        call to disagree with and no reason to fail it over its spelling."""
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "--not-yet", "x", "-C", "/tmp/scratch", "status"],
                cwd=str(CHECKOUT),
                env={},
            )
        )

    def test_another_worktree_of_the_same_repository_is_a_conflict(self):
        """The near miss `test_a_foreign_worktree_is_still_a_conflict` leaves
        open: that one uses a separate repository, so nothing about the layout
        could make it agree. A *sibling worktree's* gitdir sits under the shared
        metadata the target legitimately writes, so a prefix test reads it as
        agreement — while git takes HEAD, the index and the refs from it, and
        `git -C <A> commit` lands A's files on B's branch. Every change in this
        repository is made in its own worktree, so B is a real directory here.
        """
        root = Path(tempfile.mkdtemp())
        common = root / "primary" / ".git"
        mine, theirs = common / "worktrees" / "a", common / "worktrees" / "b"
        for made in (mine, theirs, root / "a"):
            made.mkdir(parents=True)
        (root / "a" / ".git").write_text(f"gitdir: {mine}\n", encoding="utf-8")
        (mine / "commondir").write_text("../..\n", encoding="utf-8")

        for target, name, value in (
            (root / "a", "GIT_DIR", theirs),
            (root / "a", "GIT_INDEX_FILE", theirs / "index"),
            # The primary checkout keeps `worktrees/` *inside* its own gitdir,
            # so there the sibling reads as agreement twice over.
            (root / "primary", "GIT_DIR", theirs),
        ):
            with self.subTest(target=target.name, name=name):
                complaint = _fs_sandbox.git_env_conflict(
                    ["git", "-C", str(target), "commit", "-m", "x"],
                    cwd=str(root),
                    env={name: str(value)},
                )
                assert complaint is not None
                self.assertIn("outranks -C", complaint)

    def test_a_relative_env_value_is_resolved_the_way_git_resolves_it(self):
        """`git commit` outside a worktree exports `GIT_INDEX_FILE=.git/index`,
        and git reads it *after* `-C` has changed directory — so it names the
        repository the call asked for and there is no trap to report. Resolving
        it against cwd instead refused the call and said `GIT_INDEX_FILE` was
        acting on another repository, which for a relative value is not true.
        A value that climbs back out still disagrees."""
        root = Path(tempfile.mkdtemp())
        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "-C", str(root / "scratch"), "add", "c.txt"],
                cwd=str(CHECKOUT),
                env={"GIT_INDEX_FILE": os.path.join(".git", "index")},
            )
        )
        complaint = _fs_sandbox.git_env_conflict(
            ["git", "-C", str(root / "scratch"), "add", "c.txt"],
            cwd=str(CHECKOUT),
            env={"GIT_INDEX_FILE": os.path.join("..", "elsewhere", ".git", "index")},
        )
        assert complaint is not None
        self.assertIn("outranks -C", complaint)

    def test_the_common_dir_does_not_agree_with_a_linked_worktree(self):
        """`config` lands in the common dir whichever worktree asked, which is
        the argument for calling this agreement. `config` is not the dangerous
        verb: measured against git 2.55, `GIT_DIR=<common>` with `-C <linked
        worktree>` answers `HEAD` from the *primary* checkout and
        `--show-toplevel` from the worktree, so a commit puts one tree's files
        onto the other's branch. The primary checkout, which owns the common
        dir outright, still agrees with it."""
        root = Path(tempfile.mkdtemp())
        primary, tree = root / "primary", root / "tree"
        common = primary / ".git"
        gitdir = common / "worktrees" / "tree"
        gitdir.mkdir(parents=True)
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")

        complaint = _fs_sandbox.git_env_conflict(
            ["git", "-C", str(tree), "commit", "-m", "x"],
            cwd=str(root),
            env={"GIT_DIR": str(common)},
        )
        assert complaint is not None
        self.assertIn("outranks -C", complaint)

        self.assertIsNone(
            _fs_sandbox.git_env_conflict(
                ["git", "-C", str(primary), "commit", "-m", "x"],
                cwd=str(root),
                env={"GIT_DIR": str(common)},
            )
        )

    def test_the_common_dir_is_the_same_trap(self):
        """`config` lives in the common dir, not in the per-worktree gitdir, so
        `GIT_COMMON_DIR` re-points the very file #482 was written into."""
        complaint = _fs_sandbox.git_env_conflict(
            ["git", "-C", "/tmp/scratch", "config", "user.name", "Test"],
            cwd=str(CHECKOUT),
            env={"GIT_COMMON_DIR": str(CHECKOUT / ".git")},
        )
        assert complaint is not None
        self.assertIn("GIT_COMMON_DIR", complaint)


class SubprocessHookTest(unittest.TestCase):
    """`git_env_conflict` is pure and testable on its own, but nothing reaches
    it unless `_check_subprocess` unpacks CPython's `subprocess.Popen` audit
    event correctly — `(executable, args, cwd, env)`. Get that wrong and the
    whole subprocess half is silently dead while the suite stays green, which
    is the same failure the `ENTRY_POINTS` agreement is checked for.
    """

    def test_the_audit_events_argument_order_reaches_the_rule(self):
        with self.assertRaises(_fs_sandbox.SandboxViolation):
            _fs_sandbox._check_subprocess(
                (
                    "git",
                    ["git", "-C", "/tmp/scratch", "config", "user.name", "Test"],
                    str(CHECKOUT),
                    {"GIT_DIR": str(CHECKOUT / ".git")},
                )
            )

    def test_env_none_means_the_child_inherits_ours(self):
        """The case that bit: nothing in the fixture mentioned `GIT_DIR`,
        because nothing had to."""
        with mock.patch.dict(os.environ, {"GIT_DIR": str(CHECKOUT / ".git")}):
            with self.assertRaises(_fs_sandbox.SandboxViolation):
                _fs_sandbox._check_subprocess(
                    (
                        "git",
                        ["git", "-C", "/tmp/scratch", "config", "user.name", "Test"],
                        str(CHECKOUT),
                        None,
                    )
                )

    def test_a_program_that_is_not_git_is_none_of_this_guards_business(self):
        _fs_sandbox._check_subprocess(
            (
                "/usr/bin/rsync",
                ["rsync", "-C", "/tmp/scratch", "/tmp/dest"],
                str(CHECKOUT),
                {"GIT_DIR": str(CHECKOUT / ".git")},
            )
        )

    def test_a_shell_string_has_no_argv_to_read(self):
        _fs_sandbox._check_subprocess(
            ("/bin/sh", "git -C /tmp/scratch config user.name Test", str(CHECKOUT), None)
        )


class OwnReadTest(unittest.TestCase):
    """The guard reads `<root>/.git` and the `commondir` beside it to learn
    where a target's metadata lives, and both are paths `violation` refuses. A
    probe the hook polices turns every `git -C <target outside the checkout but
    still this repo>` into a violation blaming the test for touching `.git`.
    """

    def test_a_targets_git_file_naming_this_repositorys_metadata_is_readable(self):
        root = Path(tempfile.mkdtemp())
        (root / ".git").write_text(f"gitdir: {CHECKOUT / '.git'}\n", encoding="utf-8")
        covered = _fs_sandbox._git_dirs_at(str(root))
        self.assertIn(_fs_sandbox._key(str(CHECKOUT / ".git")), covered)

    def test_the_probe_flag_is_the_only_thing_that_exempts_it(self):
        """Paired with the test above so neither can pass by the hook being
        disarmed: the same path has to be refused outside the probe."""
        self.assertIsNotNone(_fs_sandbox.violation(str(CHECKOUT / ".git" / "commondir")))


class TrackedAssetRuleTest(unittest.TestCase):
    """`asset_is_tracked` is a rule standing in for a list of ten paths, so it
    has to keep matching the list."""

    def _tracked(self) -> list[str]:
        try:
            out = subprocess.run(
                ["git", "ls-files", "assets"],
                cwd=CHECKOUT,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")
        return [line for line in out.stdout.splitlines() if line]

    def test_every_tracked_asset_matches_the_rule(self):
        tracked = self._tracked()
        self.assertTrue(tracked, "expected git to track at least the READMEs")
        for rel in tracked:
            with self.subTest(rel=rel):
                self.assertTrue(_fs_sandbox.asset_is_tracked(rel))

    def test_the_rule_matches_nothing_else_git_carries(self):
        # The other direction: a rule that said "everything under assets/" would
        # pass the test above and guard nothing.
        self.assertFalse(_fs_sandbox.asset_is_tracked("assets/videos/clip.mp4"))
        self.assertFalse(_fs_sandbox.asset_is_tracked("assets/roms/characters.901225-01.bin"))


class ArmedTest(unittest.TestCase):
    """Whether the sandbox is actually running. If these fail, the suite was
    started without `PYTHONPATH=tests` — use `make test`."""

    def test_the_hook_is_armed(self):
        self.assertTrue(
            _fs_sandbox._armed,
            "the filesystem sandbox is not armed — run the suite via `make test`, "
            "which sets PYTHONPATH=tests so tests/sitecustomize.py runs",
        )

    def test_reaching_outside_the_checkout_raises(self):
        # The file does not exist, so this asserts on the guard rather than on
        # anything in the developer's home directory.
        probe = os.path.expanduser("~/.c64cast-sandbox-probe-should-not-exist")
        with self.assertRaises(_fs_sandbox.SandboxViolation):
            with open(probe, encoding="utf-8"):
                pass

    def test_allow_outside_checkout_exempts_only_the_path_it_is_given(self):
        outside = str(Path.home() / ".c64cast-sandbox-probe-should-not-exist")
        other = str(Path.home() / ".c64cast-sandbox-other-should-not-exist")
        with _fs_sandbox.allow_outside_checkout(outside):
            _fs_sandbox._hook("open", (outside, "r", 0))  # no raise
            # The rest of the developer's home is still policed — the point of
            # taking a path instead of disarming the hook process-wide.
            with self.assertRaises(_fs_sandbox.SandboxViolation):
                _fs_sandbox._hook("open", (other, "r", 0))
        with self.assertRaises(_fs_sandbox.SandboxViolation):
            _fs_sandbox._hook("open", (outside, "r", 0))

    def test_a_bare_name_is_left_alone(self):
        # shutil.rmtree's fd-relative descent emits these, and the directory
        # they belong to is in the file descriptor, not in cwd.
        _fs_sandbox._hook("open", ("assets", "r", 0))  # no raise


class RedirectTest(unittest.TestCase):
    """The other half: the paths that *have* an override are pointed somewhere
    throwaway for every module, whether or not it asked."""

    def test_the_machine_settings_path_is_redirected(self):
        settings = paths.settings_path()
        self.assertIsNone(_fs_sandbox.violation(str(settings)))
        self.assertFalse(settings.exists(), "the machine layer must read as absent")

    def test_the_data_dir_is_redirected(self):
        self.assertIsNone(_fs_sandbox.violation(str(paths.data_root() / "anything")))

    def test_the_legacy_chargen_fallback_is_neutralized(self):
        # It is a cwd-relative path into assets/, so on a machine that has
        # dumped a character ROM there every glyph test would silently render
        # real glyphs while CI rendered the cv2 fallback.
        self.assertFalse(Path(char_rom.LEGACY_CHARGEN_PATH).is_file())


class EntryPointTest(unittest.TestCase):
    """A run with the sandbox disarmed is indistinguishable from a clean one,
    so the four places that start the suite are checked rather than trusted."""

    def test_every_entry_point_sets_pythonpath(self):
        for rel in ENTRY_POINTS:
            with self.subTest(entry_point=rel):
                body = self._code_of(CHECKOUT / rel)
                self.assertRegex(
                    body,
                    r"PYTHONPATH[:=] *tests",
                    f"{rel} starts the suite without PYTHONPATH=tests, so "
                    f"tests/sitecustomize.py never runs and the sandbox is off",
                )

    @staticmethod
    def _code_of(path: Path) -> str:
        """`path`'s body with comment lines dropped.

        Two of these four files explain the setting in a comment that quotes it
        verbatim, so a whole-file grep was satisfied by the explanation alone —
        delete the real line and the guard for the thing nothing else notices
        stayed green.
        """
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


if __name__ == "__main__":
    unittest.main()
