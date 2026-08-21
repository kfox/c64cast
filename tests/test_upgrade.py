"""Tests for c64cast.app.upgrade — install detection, the PyPI update check,
and the --upgrade command itself."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import requests

from c64cast.app import upgrade


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Swallow the conversational stdout/stderr these commands print for a
    human (confirmation prompts, "running: ..." status lines) — none of it
    is the documented behavior these tests assert on, and a test run must
    print only pass/fail/skip."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class InstallRootTest(unittest.TestCase):
    def test_install_root_is_the_repo_root_in_this_checkout(self):
        # This test file lives at <repo>/tests/, so its grandparent is the
        # answer install_root() should give from anywhere inside the package.
        self.assertEqual(upgrade.install_root(), Path(__file__).resolve().parent.parent)

    def test_running_from_checkout_detects_the_repo(self):
        self.assertTrue(upgrade.running_from_checkout())

    def test_running_from_checkout_is_false_for_an_injected_root(self):
        self.assertFalse(upgrade.running_from_checkout(root=Path("/definitely/not/a/checkout")))


class DetectInstallTest(unittest.TestCase):
    """Table-driven: install kind is entirely a function of the shape of the
    install root's path, per detect_install's documented ordering."""

    def test_checkout_wins_regardless_of_a_uv_or_pipx_looking_path(self):
        # The real repo root always has a pyproject.toml, so it must report
        # "checkout" even though this exact fixture path doesn't exist.
        install = upgrade.detect_install(root=upgrade.install_root())
        self.assertEqual(install.kind, "checkout")
        self.assertIsNone(install.command)

    def _detect(self, path: str) -> upgrade.Install:
        return upgrade.detect_install(root=Path(path))

    def test_uv_tool_install(self):
        install = self._detect(
            "/home/you/.local/share/uv/tools/c64cast/lib/python3.13/site-packages"
        )
        self.assertEqual(install.kind, "uv-tool")
        self.assertEqual(install.command, ["uv", "tool", "upgrade", "c64cast"])

    def test_pipx_install(self):
        install = self._detect("/home/you/.local/pipx/venvs/c64cast/lib/python3.13/site-packages")
        self.assertEqual(install.kind, "pipx")
        self.assertEqual(install.command, ["pipx", "upgrade", "c64cast"])

    def test_uvx_ephemeral_run_has_no_command(self):
        install = self._detect("/home/you/.cache/uv/archive-v0/abc123/lib/python3.13/site-packages")
        self.assertEqual(install.kind, "uvx")
        self.assertIsNone(install.command)

    def test_plain_pip_venv_falls_back_to_pip(self):
        install = self._detect("/home/you/venvs/myenv/lib/python3.13/site-packages")
        self.assertEqual(install.kind, "pip")
        assert install.command is not None
        self.assertEqual(install.command[1:], ["-m", "pip", "install", "--upgrade", "c64cast"])

    def test_unrecognized_shape_is_unknown(self):
        install = self._detect("/opt/weird/place")
        self.assertEqual(install.kind, "unknown")
        self.assertIsNone(install.command)


class ReleaseTupleTest(unittest.TestCase):
    def test_plain_release(self):
        self.assertEqual(upgrade._release_tuple("0.3.0"), (0, 3, 0))

    def test_double_digit_component_sorts_numerically_not_lexically(self):
        self.assertEqual(upgrade._release_tuple("0.10.0"), (0, 10, 0))

    def test_prerelease_suffix_is_dropped_from_its_component(self):
        self.assertEqual(upgrade._release_tuple("0.4.0rc1"), (0, 4, 0))

    def test_dev_suffix_component_is_dropped_entirely(self):
        self.assertEqual(upgrade._release_tuple("0.4.0.dev1"), (0, 4, 0))

    def test_uninstalled_sentinel_has_no_pure_numeric_component(self):
        # "0+unknown" split on "." is just one part; its leading digit alone
        # is a red herring — is_newer special-cases the sentinel by value
        # rather than relying on this function to reject it.
        self.assertEqual(upgrade._release_tuple("0+unknown"), (0,))

    def test_unparsable_version_is_none(self):
        self.assertIsNone(upgrade._release_tuple("bogus"))


class IsNewerTest(unittest.TestCase):
    def test_newer_release(self):
        self.assertTrue(upgrade.is_newer("0.4.0", "0.3.0"))

    def test_same_release_is_not_newer(self):
        self.assertFalse(upgrade.is_newer("0.3.0", "0.3.0"))

    def test_double_digit_component_compares_numerically(self):
        self.assertTrue(upgrade.is_newer("0.10.0", "0.3.0"))
        self.assertFalse(upgrade.is_newer("0.3.0", "0.10.0"))

    def test_stable_release_beats_a_local_prerelease_on_the_same_numbers(self):
        self.assertTrue(upgrade.is_newer("0.4.0", "0.4.0rc1"))

    def test_identical_prerelease_is_not_newer(self):
        self.assertFalse(upgrade.is_newer("0.4.0rc1", "0.4.0rc1"))

    def test_uninstalled_sentinel_cannot_be_compared(self):
        # Regression: the sentinel's leading "0" used to parse as a real
        # release segment, which made every published version look "newer"
        # than "not installed" — a true statement, but not the one
        # --check-for-updates is meant to make.
        from c64cast import UNINSTALLED_VERSION

        self.assertIsNone(upgrade.is_newer("0.3.0", UNINSTALLED_VERSION))

    def test_unparsable_version_cannot_be_compared(self):
        self.assertIsNone(upgrade.is_newer("bogus", "0.3.0"))
        self.assertIsNone(upgrade.is_newer("0.3.0", "bogus"))


class LatestReleaseTest(unittest.TestCase):
    def test_success_returns_the_version_string(self):
        response = mock.MagicMock()
        response.json.return_value = {"info": {"version": "0.4.0"}}
        with mock.patch("requests.get", return_value=response) as get:
            result = upgrade.latest_release()
        self.assertEqual(result, "0.4.0")
        response.raise_for_status.assert_called_once()
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])

    def test_connection_error_is_none(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(upgrade.latest_release())

    def test_timeout_is_none(self):
        with mock.patch("requests.get", side_effect=requests.Timeout("slow")):
            self.assertIsNone(upgrade.latest_release())

    def test_http_error_is_none(self):
        response = mock.MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("500")
        with mock.patch("requests.get", return_value=response):
            self.assertIsNone(upgrade.latest_release())

    def test_unexpected_body_shape_is_none(self):
        response = mock.MagicMock()
        response.json.return_value = {"unexpected": "shape"}
        with mock.patch("requests.get", return_value=response):
            self.assertIsNone(upgrade.latest_release())

    def test_non_json_body_is_none(self):
        response = mock.MagicMock()
        response.json.side_effect = ValueError("not json")
        with mock.patch("requests.get", return_value=response):
            self.assertIsNone(upgrade.latest_release())


class CheckoutIsDirtyTest(unittest.TestCase):
    def test_clean_tree_is_false(self):
        result = mock.MagicMock(returncode=0, stdout="")
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/git"),
            mock.patch.object(upgrade.subprocess, "run", return_value=result),
        ):
            self.assertFalse(upgrade._checkout_is_dirty(Path("/repo")))

    def test_dirty_tree_is_true(self):
        result = mock.MagicMock(returncode=0, stdout=" M some_file.py\n")
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/git"),
            mock.patch.object(upgrade.subprocess, "run", return_value=result),
        ):
            self.assertTrue(upgrade._checkout_is_dirty(Path("/repo")))

    def test_git_missing_is_none(self):
        with mock.patch.object(upgrade.shutil, "which", return_value=None):
            self.assertIsNone(upgrade._checkout_is_dirty(Path("/repo")))

    def test_git_failure_is_none(self):
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/git"),
            mock.patch.object(
                upgrade.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)
            ),
        ):
            self.assertIsNone(upgrade._checkout_is_dirty(Path("/repo")))

    def test_nonzero_returncode_is_none(self):
        # Not a git repo, or some other git-level failure — not the same
        # as "clean", so it must not be treated as safe to pull.
        result = mock.MagicMock(returncode=128, stdout="")
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/git"),
            mock.patch.object(upgrade.subprocess, "run", return_value=result),
        ):
            self.assertIsNone(upgrade._checkout_is_dirty(Path("/repo")))


class ConfirmTest(unittest.TestCase):
    def test_assume_yes_never_prompts(self):
        with mock.patch("builtins.input") as prompted:
            self.assertTrue(upgrade._confirm("Proceed?", assume_yes=True))
        prompted.assert_not_called()

    def test_non_tty_without_yes_refuses(self):
        with mock.patch.object(upgrade.sys.stdin, "isatty", return_value=False), _quiet():
            self.assertFalse(upgrade._confirm("Proceed?", assume_yes=False))

    def test_tty_yes_answer(self):
        with (
            mock.patch.object(upgrade.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="y"),
        ):
            self.assertTrue(upgrade._confirm("Proceed?", assume_yes=False))

    def test_tty_no_answer(self):
        with (
            mock.patch.object(upgrade.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="n"),
        ):
            self.assertFalse(upgrade._confirm("Proceed?", assume_yes=False))

    def test_eof_is_treated_as_no(self):
        with (
            mock.patch.object(upgrade.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", side_effect=EOFError),
        ):
            self.assertFalse(upgrade._confirm("Proceed?", assume_yes=False))


class RunCommandTest(unittest.TestCase):
    def test_missing_binary_is_exit_3(self):
        with mock.patch.object(upgrade.shutil, "which", return_value=None), _quiet():
            self.assertEqual(upgrade._run_command(["nonexistent-tool"]), 3)

    def test_timeout_is_exit_3(self):
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(
                upgrade.subprocess, "run", side_effect=subprocess.TimeoutExpired("uv", 120)
            ),
            _quiet(),
        ):
            self.assertEqual(upgrade._run_command(["uv", "tool", "upgrade", "c64cast"]), 3)

    def test_success_returns_the_process_returncode(self):
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "run", return_value=mock.MagicMock(returncode=0)),
        ):
            self.assertEqual(upgrade._run_command(["uv", "tool", "upgrade", "c64cast"]), 0)


class RunUpgradeTest(unittest.TestCase):
    def test_uvx_is_a_no_op(self):
        with (
            mock.patch.object(
                upgrade, "detect_install", return_value=upgrade.Install("uvx", Path("/x"), None)
            ),
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 0)

    def test_unknown_install_refuses(self):
        with (
            mock.patch.object(
                upgrade,
                "detect_install",
                return_value=upgrade.Install("unknown", Path("/x"), None),
            ),
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 2)

    def test_dirty_checkout_refuses_without_running_git_pull(self):
        with (
            mock.patch.object(
                upgrade,
                "detect_install",
                return_value=upgrade.Install("checkout", Path("/repo"), None),
            ),
            mock.patch.object(upgrade, "_checkout_is_dirty", return_value=True),
            mock.patch.object(upgrade, "_run_command") as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 2)
        run_command.assert_not_called()

    def test_unverifiable_checkout_refuses_like_dirty(self):
        with (
            mock.patch.object(
                upgrade,
                "detect_install",
                return_value=upgrade.Install("checkout", Path("/repo"), None),
            ),
            mock.patch.object(upgrade, "_checkout_is_dirty", return_value=None),
            mock.patch.object(upgrade, "_run_command") as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 2)
        run_command.assert_not_called()

    def test_clean_checkout_pulls_then_syncs(self):
        with (
            mock.patch.object(
                upgrade,
                "detect_install",
                return_value=upgrade.Install("checkout", Path("/repo"), None),
            ),
            mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
            mock.patch.object(upgrade, "_run_command", return_value=0) as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 0)
        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(commands, [["git", "pull"], ["uv", "sync", "--all-extras"]])

    def test_clean_checkout_stops_if_pull_fails(self):
        with (
            mock.patch.object(
                upgrade,
                "detect_install",
                return_value=upgrade.Install("checkout", Path("/repo"), None),
            ),
            mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
            mock.patch.object(upgrade, "_run_command", return_value=1) as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 1)
        # Only `git pull` ran — `uv sync` must not run over a failed pull.
        run_command.assert_called_once_with(["git", "pull"], cwd=Path("/repo"))

    def test_declining_the_prompt_refuses_without_running_anything(self):
        install = upgrade.Install("uv-tool", Path("/x"), ["uv", "tool", "upgrade", "c64cast"])
        with (
            mock.patch.object(upgrade, "detect_install", return_value=install),
            mock.patch.object(upgrade, "_confirm", return_value=False),
            mock.patch.object(upgrade, "_run_command") as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=False), 2)
        run_command.assert_not_called()

    def test_uv_tool_runs_its_command_once_confirmed(self):
        install = upgrade.Install("uv-tool", Path("/x"), ["uv", "tool", "upgrade", "c64cast"])
        with (
            mock.patch.object(upgrade, "detect_install", return_value=install),
            mock.patch.object(upgrade, "_run_command", return_value=0) as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 0)
        run_command.assert_called_once_with(["uv", "tool", "upgrade", "c64cast"])

    def test_pipx_runs_its_command_once_confirmed(self):
        install = upgrade.Install("pipx", Path("/x"), ["pipx", "upgrade", "c64cast"])
        with (
            mock.patch.object(upgrade, "detect_install", return_value=install),
            mock.patch.object(upgrade, "_run_command", return_value=0) as run_command,
            _quiet(),
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 0)
        run_command.assert_called_once_with(["pipx", "upgrade", "c64cast"])


if __name__ == "__main__":
    unittest.main()
