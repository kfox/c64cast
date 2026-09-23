"""Tests for `.claude/hooks/_shell.py` — the one reader the `PreToolUse(Bash)`
hooks use to work out which commands a command line runs.

A hook that anchors on a command's leading word can only be as good as the
split that produced the command, and `shlex.split` does not treat punctuation
as a token: `echo hi;grep -rn x docs/` lexes `hi;grep` as one word, so the
line looks like a single `echo` and every hook downstream stays silent. The
same hole swallows `&&`, `||`, `|`, `&`, a newline, a subshell and a command
substitution, in the glued spelling and the spaced one alike.

The failure directions differ by hook, which is why the reader hands back the
wiring rather than deciding anything with it. For `require-worktrees-in-
checkout.py` a miss is a stray worktree and the costly direction is the false
allow; for the three redirect hooks a miss costs a nudge and the costly
direction is the false deny, so they pass over output that goes to a pipe or
a file and decline to read a line carrying a substitution at all.

The hooks are loaded by path: `.claude/hooks/` is not a package, and the
filenames are not identifiers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / ".claude" / "hooks"


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_without_the_hooks_directory_on_the_path(filename: str, name: str):
    """`filename` loaded with its own directory absent from `sys.path` and no
    `_shell` already imported.

    `spec_from_file_location` does not put the script's directory on the path
    the way running it does, so this is the load that fails if a hook stops
    arranging its own import. A module-level `ImportError` in a `PreToolUse`
    hook is the silent-lapse direction: the hook exits non-zero, and the
    registration form in `settings.json` decides whether that blocks the
    session or merely turns the guard off.
    """
    saved_path = list(sys.path)
    saved_shell = sys.modules.pop("_shell", None)
    sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != _HOOKS]
    try:
        return _load(filename, name)
    finally:
        sys.path = saved_path
        sys.modules.pop("_shell", None)
        if saved_shell is not None:
            sys.modules["_shell"] = saved_shell


sys.path.insert(0, str(_HOOKS))
import _shell  # noqa: E402

to_uv = _load("redirect-to-uv.py", "redirect_to_uv")
to_make_test = _load("redirect-to-make-test.py", "redirect_to_make_test")
bash_search = _load("redirect-bash-search.py", "redirect_bash_search")
search_target = _load("require-resolvable-search-target.py", "require_resolvable_search_target")
worktrees = _load("require-worktrees-in-checkout.py", "require_worktrees_in_checkout")

# The working directory the hooks are asked from. It is not the one running
# the suite, so a destination resolved against it cannot accidentally land
# inside this checkout and be allowed for the wrong reason.
_CWD = "/private/tmp/some-checkout"


def _uv_verdict(cmd: str) -> str | None:
    return next((r for r in (to_uv.verdict(c.argv) for c in _shell.read(cmd).commands) if r), None)


def _make_test_verdict(cmd: str) -> str | None:
    fires = any(to_make_test._is_raw_test_run(c.argv) for c in _shell.read(cmd).commands)
    return to_make_test.DENY if fires else None


class ReadingTest(unittest.TestCase):
    def argvs(self, cmd: str) -> list[list[str]]:
        return [command.argv for command in _shell.read(cmd).commands]

    def test_a_separator_the_shell_needs_no_space_around_still_splits(self):
        for cmd in (
            "echo hi;uv pip install foo",
            "echo hi&&uv pip install foo",
            "echo hi||uv pip install foo",
            "true|uv pip install foo",
            "echo hi&uv pip install foo",
        ):
            self.assertEqual(self.argvs(cmd)[-1], ["uv", "pip", "install", "foo"], cmd)

    def test_a_newline_separates_commands_rather_than_joining_them(self):
        # shlex treats a newline as whitespace, which fuses the two lines into
        # one argv and hides whichever command came second.
        self.assertEqual(
            self.argvs("echo hi\nuv pip install foo"),
            [["echo", "hi"], ["uv", "pip", "install", "foo"]],
        )

    def test_a_subshell_and_a_substitution_are_commands_of_their_own(self):
        self.assertEqual(self.argvs("(uv pip install foo)"), [["uv", "pip", "install", "foo"]])
        self.assertEqual(
            self.argvs("echo $(uv pip install foo)"),
            [["echo"], ["uv", "pip", "install", "foo"]],
        )
        self.assertEqual(
            self.argvs("echo `uv pip install foo`"),
            [["echo"], ["uv", "pip", "install", "foo"]],
        )

    def test_only_a_substitution_is_marked_as_one(self):
        self.assertTrue(_shell.read("echo $(ls)").has_substitution())
        self.assertTrue(_shell.read("echo `ls`").has_substitution())
        self.assertFalse(_shell.read("(ls)").has_substitution())
        self.assertFalse(_shell.read("ls | head").has_substitution())

    def test_a_substitution_glued_to_the_word_before_it_is_still_one(self):
        # shlex leaves the `$` on the word it touches, so `files=$(` arrives
        # as `files=$` and only a bare `$` would be recognized. The captured
        # command's output goes into the variable, never to the caller.
        reading = _shell.read("files=$(grep -rn needle docs/)")
        self.assertTrue(reading.has_substitution())
        self.assertEqual(reading.commands[-1].argv, ["grep", "-rn", "needle", "docs/"])

    def test_a_substitution_left_open_by_a_line_carries_to_the_next(self):
        self.assertTrue(_shell.read("echo $(\ngrep -rn needle docs/\n)").has_substitution())

    def test_a_punctuation_cluster_is_split_into_the_operators_in_it(self):
        self.assertEqual(_shell.split_cluster(";("), [";", "("])
        self.assertEqual(_shell.split_cluster("&&("), ["&&", "("])
        self.assertEqual(_shell.split_cluster(")&&"), [")", "&&"])
        self.assertEqual(_shell.split_cluster("&&"), ["&&"])

    def test_a_word_is_not_mistaken_for_a_cluster(self):
        self.assertEqual(_shell.split_cluster("docs/"), ["docs/"])
        self.assertEqual(_shell.split_cluster(""), [""])

    def test_a_pipe_marks_both_sides(self):
        left, right = _shell.read("grep -r x docs/ | head -20").commands
        self.assertEqual((left.stdout_to_pipe, left.stdin_from_pipe), (True, False))
        self.assertEqual((right.stdout_to_pipe, right.stdin_from_pipe), (False, True))

    def test_a_redirect_of_stdout_is_told_from_a_redirect_of_stderr(self):
        # `2>` is the file descriptor and the operator lexed apart, and the
        # descriptor is what says whether the output a caller cares about
        # still reaches the terminal.
        (to_file,) = _shell.read("grep -r x docs/ > out.txt").commands
        (to_terminal,) = _shell.read("grep -r x docs/ 2>/dev/null").commands
        self.assertTrue(to_file.stdout_to_file)
        self.assertFalse(to_terminal.stdout_to_file)

    def test_stdout_handed_to_another_descriptor_is_not_a_file(self):
        # `>&2` puts every line on stderr, which the caller reads back just
        # as it reads stdout; `>&out.txt` and `&>out.txt` name a file.
        (dup,) = _shell.read("grep -r x docs/ >&2").commands
        self.assertFalse(dup.stdout_to_file)
        for cmd in ("grep -r x docs/ >&out.txt", "grep -r x docs/ &>out.txt"):
            (to_file,) = _shell.read(cmd).commands
            self.assertTrue(to_file.stdout_to_file, cmd)

    def test_a_pipe_after_a_group_marks_what_ran_inside_it(self):
        # `)` and `}` leave a placeholder command behind them, and marking
        # that one would leave the grep looking unpiped.
        for cmd in ("(grep -r x docs/) | head -5", "{ grep -r x docs/; } | head -5"):
            piped = [c for c in _shell.read(cmd).commands if "grep" in c.argv]
            self.assertEqual([c.stdout_to_pipe for c in piped], [True], cmd)

    def test_a_redirect_after_a_group_marks_what_ran_inside_it(self):
        (inside,) = _shell.read("(grep -r x docs/) > out.txt").commands
        self.assertTrue(inside.stdout_to_file)

    def test_an_operand_of_the_command_is_not_read_as_a_descriptor(self):
        # The digit only names a descriptor when it is glued to the operator;
        # a `)` in between makes it an argument the command keeps.
        (command,) = _shell.read("(echo 2) > out.txt").commands
        self.assertEqual((command.argv, command.stdout_to_file), (["echo", "2"], True))

    def test_a_redirects_file_descriptor_does_not_stay_in_the_argv(self):
        (command,) = _shell.read("git worktree move 2>/dev/null a b").commands
        self.assertEqual(command.argv, ["git", "worktree", "move", "a", "b"])

    def test_a_heredoc_body_is_held_by_the_command_that_opened_it(self):
        (command,) = _shell.read("record x <<'EOF'\nbody line\nEOF").commands
        self.assertEqual((command.argv, command.heredoc_body), (["record", "x"], "body line"))

    def test_a_heredoc_body_is_not_read_as_commands(self):
        self.assertEqual(self.argvs("cat <<'EOF'\nuv pip install foo\nEOF"), [["cat"]])

    def test_a_command_after_a_heredoc_terminator_is_read(self):
        self.assertEqual(
            self.argvs("cat <<'EOF' > f.md\nprose\nEOF\nuv pip install foo"),
            [["cat"], ["uv", "pip", "install", "foo"]],
        )

    def test_an_unterminated_quote_comes_back_as_the_unreadable_tail(self):
        self.assertEqual(_shell.read("echo 'never closed").unreadable, "echo 'never closed")

    def test_a_quote_that_spans_lines_is_joined_rather_than_dropped(self):
        self.assertEqual(
            self.argvs('git commit -m "subject\n\nbody" && uv pip install foo'),
            [["git", "commit", "-m", "subject\n\nbody"], ["uv", "pip", "install", "foo"]],
        )

    def test_a_line_continuation_is_read_as_one_command(self):
        self.assertEqual(self.argvs("uv pip \\\n  install foo"), [["uv", "pip", "install", "foo"]])

    def test_a_leading_assignment_or_keyword_is_not_the_command(self):
        self.assertEqual(_shell.strip_prefix(["A=1", "B=2", "make", "test"]), ["make", "test"])
        self.assertEqual(
            _shell.strip_prefix(["if", "grep", "-q", "x", "f"]), ["grep", "-q", "x", "f"]
        )
        self.assertEqual(_shell.strip_prefix(["then", "make", "test"]), ["make", "test"])
        self.assertEqual(_shell.strip_prefix(["make", "test"]), ["make", "test"])


class GluedSeparatorTest(unittest.TestCase):
    """Every hook that anchors on a command's leading word sees the command.

    The spellings are the ones a shell accepts without a space, which is what
    made the miss silent: the line looks well-formed and the hook says
    nothing.
    """

    def test_the_package_manager_hook_sees_a_glued_install(self):
        for cmd in (
            "echo hi;uv pip install foo",
            "echo hi&&pip install foo",
            "true|uv pip install foo",
            "echo hi&pip3 install foo",
            "(uv pip install foo)",
            "echo $(uv pip install foo)",
            "echo hi\nuv pip install foo",
        ):
            self.assertIsNotNone(_uv_verdict(cmd), cmd)

    def test_the_package_manager_hook_sees_a_glued_checker(self):
        for cmd in ("echo hi;mypy c64cast", "echo hi&&ruff check .", "true|pyright"):
            self.assertIsNotNone(_uv_verdict(cmd), cmd)

    def test_the_package_manager_hook_still_passes_the_sanctioned_forms(self):
        for cmd in (
            "make check",
            "echo hi;make check",
            "uv run python -m c64cast --help",
            "echo hi && uv sync --all-extras",
        ):
            self.assertIsNone(_uv_verdict(cmd), cmd)

    def test_the_test_runner_hook_sees_a_glued_runner(self):
        for cmd in (
            "echo hi;pytest tests",
            "echo hi&&pytest tests",
            "true|pytest tests",
            "echo hi;python -m unittest tests.test_api",
        ):
            self.assertIsNotNone(_make_test_verdict(cmd), cmd)

    def test_the_test_runner_hook_still_passes_make(self):
        for cmd in ("make test", "echo hi;make test", "make test T=tests.test_api"):
            self.assertIsNone(_make_test_verdict(cmd), cmd)

    def test_the_search_hook_sees_a_glued_unbounded_search(self):
        for cmd in (
            "echo hi;grep -rn needle docs/",
            "echo hi; grep -rn needle docs/",
            "echo hi&&grep -rn needle docs/",
            "echo hi\ngrep -rn needle docs/",
            "echo hi;cat pyproject.toml",
        ):
            self.assertIsNotNone(bash_search.line_verdict(cmd), cmd)

    def test_the_search_hook_passes_output_that_never_reaches_the_context(self):
        for cmd in (
            "grep -rn needle docs/ | head -20",
            "grep -rn needle docs/ > /dev/null",
            "cat pyproject.toml | head -5",
            "echo hi;grep -rn needle docs/ | head -20",
        ):
            self.assertIsNone(bash_search.line_verdict(cmd), cmd)

    def test_the_search_hook_passes_a_grep_that_prints_nothing(self):
        # `-q` exits with the answer and writes no matches at all, so the
        # bound this hook asks for is already there.
        for cmd in ("grep -rq needle docs/", "grep -r --quiet needle docs/"):
            self.assertIsNone(bash_search.line_verdict(cmd), cmd)

    def test_the_search_hook_declines_a_line_carrying_a_substitution(self):
        for cmd in (
            "echo $(grep -rn needle docs/)",
            "files=$(grep -rn needle docs/)",
            "echo $(\ngrep -rn needle docs/\n)",
        ):
            self.assertIsNone(bash_search.line_verdict(cmd), cmd)

    def test_the_search_hook_passes_a_group_whose_output_is_piped(self):
        for cmd in (
            "(grep -rn needle docs/) | head -20",
            "{ grep -rn needle docs/; } | head -20",
            "(grep -rn needle docs/) > /dev/null",
        ):
            self.assertIsNone(bash_search.line_verdict(cmd), cmd)

    def test_the_search_hook_sees_output_handed_to_stderr(self):
        # `>&2` reaches the caller exactly as stdout does, so the spill this
        # hook exists to catch is still a spill.
        self.assertIsNotNone(bash_search.line_verdict("grep -rn needle docs/ >&2"))

    def test_the_resolvable_target_hook_sees_a_glued_search_after_a_cd(self):
        for cmd in (
            "cd /private/tmp;grep -rn foo src/",
            "cd /private/tmp&&grep -rn foo src/",
            "cd /private/tmp\ngrep -rn foo src/",
        ):
            self.assertIsNotNone(search_target.verdict(cmd), cmd)

    def test_the_resolvable_target_hook_still_passes_a_resolvable_one(self):
        for cmd in (
            "cd /private/tmp && grep -rn foo /abs/path",
            "grep -rn foo src/",
            "cd /private/tmp && cat f | grep foo",
        ):
            self.assertIsNone(search_target.verdict(cmd), cmd)

    def test_the_worktree_hook_sees_a_glued_add(self):
        for cmd in (
            "echo hi;git worktree add /private/tmp/x",
            "echo hi&&git worktree add /private/tmp/x",
            "echo hi\ngit worktree add /private/tmp/x",
        ):
            self.assertIsNotNone(worktrees.verdict(cmd, _CWD), cmd)


class HookImportTest(unittest.TestCase):
    """Each hook arranges the import of the shared reader itself.

    Running `python3 <abs-path>` puts the script's directory on `sys.path`,
    so a hook that leaned on that alone would work in Claude Code and fail
    under every other loader — including the one these tests use. The failure
    would be a module-level `ImportError`, which is the quiet direction: the
    guard stops guarding and says nothing about it.
    """

    def test_every_bash_hook_imports_with_its_directory_off_the_path(self):
        for filename in sorted(p.name for p in _HOOKS.glob("*.py") if not p.name.startswith("_")):
            with self.subTest(hook=filename):
                module = _load_without_the_hooks_directory_on_the_path(
                    filename, f"isolated_{filename.replace('-', '_')[:-3]}"
                )
                self.assertTrue(callable(module.main))


class HookProtocolTest(unittest.TestCase):
    """The reader's new answers reach the wire through `main()` unchanged."""

    def _main(self, module, cmd: str) -> tuple[int, str]:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": _CWD})
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": _CWD}):
                code = module.main()
        return code, out.getvalue()

    def test_a_glued_command_reaches_a_deny_decision(self):
        for module, cmd in (
            (to_uv, "echo hi;uv pip install foo"),
            (to_make_test, "echo hi;pytest tests"),
            (bash_search, "echo hi;grep -rn needle docs/"),
            (search_target, "cd /private/tmp;grep -rn foo src/"),
        ):
            with self.subTest(hook=module.__name__):
                code, printed = self._main(module, cmd)
                self.assertEqual(code, 0)
                decision = json.loads(printed)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_an_allowed_command_prints_nothing(self):
        for module in (to_uv, to_make_test, bash_search, search_target):
            with self.subTest(hook=module.__name__):
                self.assertEqual(self._main(module, "make check"), (0, ""))


if __name__ == "__main__":
    unittest.main()
