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
