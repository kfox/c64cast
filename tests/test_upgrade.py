"""Tests for c64cast.app.upgrade — install detection, the PyPI update check,
and the --upgrade command itself."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
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

    def test_non_adjacent_uv_and_tools_segments_are_not_a_uv_tool_install(self):
        # A hand-made pip venv that happens to sit under a directory called
        # `tools` beside one called `uv`. Unordered membership called this a
        # uv-tool install and printed the wrong installer's command.
        install = self._detect("/home/you/tools/uv/venv/lib/python3.13/site-packages")
        self.assertEqual(install.kind, "pip")

    def test_non_adjacent_pipx_and_venvs_segments_are_not_a_pipx_install(self):
        install = self._detect("/home/you/pipx/mine/venvs/lib/python3.13/site-packages")
        self.assertEqual(install.kind, "pip")


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


class LooksLikeVersionTest(unittest.TestCase):
    """The gate both trust boundaries share: PyPI's `info.version`, and the
    two versions `update_state` reads back out of a file a lower-privileged
    account may own."""

    def test_plain_and_suffixed_releases_pass(self):
        for value in ("0.4.0", "0.4.0rc1", "1.0.0.dev3", "0.4.0+local", "0+unknown"):
            with self.subTest(value=value):
                self.assertTrue(upgrade.looks_like_version(value))

    def test_a_non_string_is_not_a_version(self):
        for value in (None, 5, 0.4, ["0.4.0"], {"version": "0.4.0"}, True):
            with self.subTest(value=value):
                self.assertFalse(upgrade.looks_like_version(value))

    def test_control_characters_and_whitespace_are_not_a_version(self):
        for value in ("0.5.0\nSECURITY: run curl | sudo sh", "0.5.0\x1b[2J", "0.5.0 ", ""):
            with self.subTest(value=value):
                self.assertFalse(upgrade.looks_like_version(value))

    def test_an_implausibly_long_token_is_not_a_version(self):
        self.assertFalse(upgrade.looks_like_version("0." * 40))


class LatestReleaseTest(unittest.TestCase):
    """Documented "never raises", and — since a wrong answer here becomes a
    login banner offering a release that does not exist — never guesses."""

    def _fetch_answering(self, response: mock.MagicMock) -> str | None:
        with mock.patch("requests.get", return_value=response):
            return upgrade.latest_release()

    def _assert_none_with_a_breadcrumb(self, response: mock.MagicMock) -> None:
        # The one diagnostic distinguishing a DNS failure from a proxy's 403
        # from a change in the shape of `info.version`, all of which return
        # the same None.
        with self.assertLogs("c64cast.app.upgrade", level="DEBUG"):
            self.assertIsNone(self._fetch_answering(response))

    def test_success_returns_the_version_string(self):
        response = mock.MagicMock()
        response.json.return_value = {"info": {"version": "0.4.0"}}
        with mock.patch("requests.get", return_value=response) as get:
            result = upgrade.latest_release()
        self.assertEqual(result, "0.4.0")
        response.raise_for_status.assert_called_once()
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])

    def test_connection_error_is_none(self):
        with (
            self.assertLogs("c64cast.app.upgrade", level="DEBUG"),
            mock.patch("requests.get", side_effect=requests.ConnectionError("down")),
        ):
            self.assertIsNone(upgrade.latest_release())

    def test_timeout_is_none(self):
        with (
            self.assertLogs("c64cast.app.upgrade", level="DEBUG"),
            mock.patch("requests.get", side_effect=requests.Timeout("slow")),
        ):
            self.assertIsNone(upgrade.latest_release())

    def test_http_error_is_none(self):
        response = mock.MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("500")
        self._assert_none_with_a_breadcrumb(response)

    def test_unexpected_body_shape_is_none(self):
        response = mock.MagicMock()
        response.json.return_value = {"unexpected": "shape"}
        self._assert_none_with_a_breadcrumb(response)

    def test_non_json_body_is_none(self):
        response = mock.MagicMock()
        response.json.side_effect = ValueError("not json")
        self._assert_none_with_a_breadcrumb(response)

    def test_a_body_that_is_not_a_json_object_is_none(self):
        # `r.json()["info"]` raised TypeError, which the enumerated except
        # tuple did not cover, straight out of a "never raises" function.
        for body in ([], "0.4.0", 5, None):
            with self.subTest(body=body):
                response = mock.MagicMock()
                response.json.return_value = body
                self._assert_none_with_a_breadcrumb(response)

    def test_a_null_info_object_is_none(self):
        response = mock.MagicMock()
        response.json.return_value = {"info": None}
        self._assert_none_with_a_breadcrumb(response)

    def test_a_non_string_version_is_not_coerced_into_an_answer(self):
        # `str()` made these into answers nothing downstream could catch:
        # `None` became the release "None", and `5` became "5", which
        # compares newer than every version this project has published.
        for version in (None, 5, ["0.4.0"], True):
            with self.subTest(version=version):
                response = mock.MagicMock()
                response.json.return_value = {"info": {"version": version}}
                self._assert_none_with_a_breadcrumb(response)

    def test_a_version_carrying_control_characters_is_rejected(self):
        response = mock.MagicMock()
        response.json.return_value = {"info": {"version": "0.5.0\nSECURITY: curl | sudo sh"}}
        self._assert_none_with_a_breadcrumb(response)

    def test_a_broken_requests_install_is_none_not_an_import_error(self):
        # The module's whole lazy-import discipline exists so --upgrade
        # survives a broken hard dependency; the import sat outside the try.
        with (
            self.assertLogs("c64cast.app.upgrade", level="DEBUG"),
            mock.patch.dict("sys.modules", {"requests": None}),
        ):
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


def _fake_process(*waits: object) -> mock.MagicMock:
    """A Popen stand-in whose context manager yields itself, so
    `_run_command`'s `with subprocess.Popen(...)` sees it.

    `waits` are the successive `wait()` outcomes — an exception instance is
    raised, anything else returned — with the last one repeating, since
    `Popen.__exit__` waits once more on the way out."""
    process = mock.MagicMock()
    process.__enter__.return_value = process
    process.__exit__.return_value = False
    outcomes = list(waits) or [0]

    def wait(*_args: object, **_kwargs: object) -> object:
        outcome = outcomes[0] if len(outcomes) == 1 else outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    process.wait.side_effect = wait
    return process


class UpgradeTimeoutTest(unittest.TestCase):
    """The ceiling on an install command is generous and overridable: a
    release that moves an opencv/PyAV/numpy pin can build from source for an
    hour on a Pi-class host, and stopping that partway through replacing
    site-packages is how the repair command breaks an install."""

    def test_the_default_is_far_more_than_a_package_download(self):
        with mock.patch.dict(upgrade.os.environ):
            upgrade.os.environ.pop(upgrade.UPGRADE_TIMEOUT_ENV, None)
            timeout = upgrade._upgrade_timeout_s()
        assert timeout is not None
        self.assertGreaterEqual(timeout, 1800.0)

    def test_the_env_var_overrides_it(self):
        with mock.patch.dict(upgrade.os.environ, {upgrade.UPGRADE_TIMEOUT_ENV: "45"}):
            self.assertEqual(upgrade._upgrade_timeout_s(), 45.0)

    def test_zero_removes_the_ceiling(self):
        with mock.patch.dict(upgrade.os.environ, {upgrade.UPGRADE_TIMEOUT_ENV: "0"}):
            self.assertIsNone(upgrade._upgrade_timeout_s())

    def test_an_unparsable_value_is_reported_and_ignored(self):
        # Never read as 0: silently removing the ceiling is the one reading a
        # typo must not get.
        with mock.patch.dict(upgrade.os.environ, {upgrade.UPGRADE_TIMEOUT_ENV: "soon"}):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(upgrade._upgrade_timeout_s(), upgrade._UPGRADE_TIMEOUT_S)
        self.assertIn(upgrade.UPGRADE_TIMEOUT_ENV, err.getvalue())


class RunCommandTest(unittest.TestCase):
    def test_missing_binary_is_exit_3(self):
        with mock.patch.object(upgrade.shutil, "which", return_value=None), _quiet():
            self.assertEqual(upgrade._run_command(["nonexistent-tool"]), 3)

    def test_the_resolved_path_is_what_gets_executed(self):
        # shutil.which's answer used to be discarded and the unqualified name
        # handed to exec, which re-resolves PATH — so the binary that was
        # checked was not provably the one that ran.
        process = _fake_process(0)
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/local/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", return_value=process) as popen,
        ):
            self.assertEqual(upgrade._run_command(["uv", "tool", "upgrade", "c64cast"]), 0)
        self.assertEqual(
            popen.call_args.args[0], ["/usr/local/bin/uv", "tool", "upgrade", "c64cast"]
        )

    def test_a_spawn_failure_is_exit_3(self):
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", side_effect=OSError("no exec")),
            _quiet(),
        ):
            self.assertEqual(upgrade._run_command(["uv", "tool", "upgrade", "c64cast"]), 3)

    def test_a_command_over_the_ceiling_is_interrupted_before_it_is_killed(self):
        # SIGKILL partway through replacing site-packages is what turns the
        # repair command into the thing that broke the install.
        expired = subprocess.TimeoutExpired("uv", 1)
        process = _fake_process(expired, expired, -9)
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", return_value=process),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(upgrade._run_command(["uv", "sync", "--all-extras"]), 3)
        if upgrade.os.name == "posix":
            process.send_signal.assert_called_once_with(upgrade.signal.SIGINT)
        else:
            process.terminate.assert_called_once()
        process.kill.assert_called_once()
        # The user has to be told what state the install is in.
        self.assertIn("only part of the upgrade", err.getvalue())
        self.assertIn(upgrade.UPGRADE_TIMEOUT_ENV, err.getvalue())

    def test_a_command_that_unwinds_on_the_interrupt_is_not_killed(self):
        process = _fake_process(subprocess.TimeoutExpired("uv", 1), -2)
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", return_value=process),
            _quiet(),
        ):
            self.assertEqual(upgrade._run_command(["uv", "sync", "--all-extras"]), 3)
        process.kill.assert_not_called()

    def test_a_keyboard_interrupt_stops_the_child_before_it_unwinds(self):
        # `subprocess.run` killed the child on any exception; without that, a
        # child that shrugs off the terminal's Ctrl-C would be waited on
        # forever by Popen.__exit__.
        process = _fake_process(KeyboardInterrupt(), 0)
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", return_value=process),
            self.assertRaises(KeyboardInterrupt),
        ):
            upgrade._run_command(["uv", "sync", "--all-extras"])
        self.assertTrue(process.send_signal.called or process.terminate.called)

    def test_success_returns_the_process_returncode(self):
        process = _fake_process(0)
        with (
            mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(upgrade.subprocess, "Popen", return_value=process),
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

    def test_an_install_kind_with_no_upgrade_command_names_itself(self):
        # A `command is None` test made this branch print "could not tell how
        # this install was made", which is wrong for a kind someone has just
        # finished enumerating.
        install = upgrade.Install("pipx", Path("/x"), None)
        with (
            mock.patch.object(upgrade, "detect_install", return_value=install),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(upgrade.run_upgrade(assume_yes=True), 2)
        self.assertIn("pipx", err.getvalue())
        self.assertNotIn("could not tell how", err.getvalue())

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


class UpgradeCheckoutTest(unittest.TestCase):
    """The checkout branch refuses four ways before it moves any source, and
    each refusal has to name the cause it actually found."""

    @contextlib.contextmanager
    def _checkout(self, *, with_git: bool = True) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pyproject.toml").write_text("[project]\nname = 'c64cast'\n")
            if with_git:
                (root / ".git").mkdir()
            yield root

    def test_a_source_tree_with_no_git_repository_says_so(self):
        # pyproject.toml ships in the sdist, so an unpacked release archive
        # lands in this branch. `git status` exits 128 there, which read as
        # "could not be checked (is git on PATH?)" and sent the user off to
        # fix a PATH that was never the problem.
        with self._checkout(with_git=False) as root:
            with (
                mock.patch.object(upgrade, "_run_command") as run_command,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 2)
        run_command.assert_not_called()
        self.assertIn("no git repository", err.getvalue())
        self.assertNotIn("is git on PATH", err.getvalue())

    def test_a_dirty_tree_refuses_and_says_to_commit_or_stash(self):
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=True),
                mock.patch.object(upgrade, "_run_command") as run_command,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 2)
        run_command.assert_not_called()
        self.assertIn("uncommitted changes", err.getvalue())

    def test_an_unverifiable_tree_refuses_without_the_dirty_tree_advice(self):
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=None),
                mock.patch.object(upgrade, "_run_command") as run_command,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 2)
        run_command.assert_not_called()
        self.assertIn("is git on PATH", err.getvalue())
        self.assertNotIn("Commit or stash", err.getvalue())

    def test_a_missing_uv_refuses_before_anything_is_pulled(self):
        # `_run_command`'s own check is per-command, so a missing uv used to
        # surface only after `git pull` had moved the source — leaving the
        # tree on new code with the old dependency set.
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
                mock.patch.object(upgrade.shutil, "which", return_value=None),
                mock.patch.object(upgrade, "_run_command") as run_command,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 2)
        run_command.assert_not_called()
        self.assertIn("'uv' is not on PATH", err.getvalue())

    def test_a_clean_tree_pulls_then_syncs(self):
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
                mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
                mock.patch.object(upgrade, "_run_command", return_value=0) as run_command,
                _quiet(),
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 0)
        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(commands, [["git", "pull"], ["uv", "sync", "--all-extras"]])

    def test_a_failed_pull_stops_before_the_sync(self):
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
                mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
                mock.patch.object(upgrade, "_run_command", return_value=1) as run_command,
                _quiet(),
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=True), 1)
            run_command.assert_called_once_with(["git", "pull"], cwd=root)

    def test_declining_the_prompt_pulls_nothing(self):
        with self._checkout() as root:
            with (
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
                mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
                mock.patch.object(upgrade, "_confirm", return_value=False),
                mock.patch.object(upgrade, "_run_command") as run_command,
                _quiet(),
            ):
                self.assertEqual(upgrade._upgrade_checkout(root, assume_yes=False), 2)
        run_command.assert_not_called()

    def test_run_upgrade_routes_a_checkout_to_this_branch(self):
        with self._checkout() as root:
            with (
                mock.patch.object(
                    upgrade,
                    "detect_install",
                    return_value=upgrade.Install("checkout", root, None),
                ),
                mock.patch.object(upgrade, "_checkout_is_dirty", return_value=False),
                mock.patch.object(upgrade.shutil, "which", return_value="/usr/bin/uv"),
                mock.patch.object(upgrade, "_run_command", return_value=0) as run_command,
                _quiet(),
            ):
                self.assertEqual(upgrade.run_upgrade(assume_yes=True), 0)
        self.assertEqual(run_command.call_count, 2)


if __name__ == "__main__":
    unittest.main()
