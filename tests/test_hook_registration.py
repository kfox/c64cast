"""Tests that `.claude/settings.json` and `.claude/hooks/` still agree.

Each hook has tests for its own logic, and every one of them passes just as
well when nothing runs the hook. A hook dropped from `settings.json`, or
registered under a path with no script behind it, leaves the suite green and
the guard gone — the silent lapse the hooks themselves exist to close, turned
on the hooks.

Three agreements are pinned, and the third is why a hook's scope can change
without editing this file. Every hook script is registered; every
registration names a script that is there; and the tool each one is
registered against is the tool its own docstring says it is for. That last
one is a real failure on its own — a `Bash` guard registered against `Write`
is off for every command — but hard-coding the matchers here would mean
editing a test whenever a scope legitimately changes. The hooks already
declare their event in their first docstring line, so the declaration is what
this checks against, and a scope change stays one edit in one file.

Nothing here imports a hook or runs one: `settings.json` is read as JSON, the
directory is listed, and the docstrings come from `ast`.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from _child_process import run_bounded

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO_ROOT / ".claude" / "hooks"
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"
_POSIX = os.name == "posix"

# The path a registration names, wherever it sits in the shell command that
# runs it — the command mentions it as a variable assignment, as an argument,
# or both.
_SCRIPT_IN_A_COMMAND = re.compile(r"\.claude/hooks/([A-Za-z0-9_.-]+\.py)")

# What a hook's first docstring line declares it is for, e.g.
# `PreToolUse(Edit|Write|NotebookEdit) hook — keep edits out of …`.
_DECLARED_EVENT = re.compile(r"^(\w+)\(([^)]*)\)\s+hook\b")


def _settings() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


def _commands() -> list[str]:
    """Every shell command `settings.json` runs as a hook."""
    return [
        hook["command"]
        for entries in _settings()["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _guarded(script: str) -> str:
    """The registration a hook script gets: run it if it is there, and say
    nothing at all if it is not."""
    path = f"$CLAUDE_PROJECT_DIR/.claude/hooks/{script}"
    return f'f="{path}"; [ -f "$f" ] || exit 0; exec python3 "$f"'


def _command_for(script: str) -> str:
    """The command `settings.json` registers for `script`, as written."""
    for command in _commands():
        if script in _SCRIPT_IN_A_COMMAND.findall(command):
            return command
    raise AssertionError(f"{script} is not registered in {_SETTINGS}")


def _run(command: str, project_dir: str, payload: str) -> subprocess.CompletedProcess[str]:
    """`command` run the way Claude Code runs a `type: "command"` hook."""
    return run_bounded(
        ["/bin/sh", "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": project_dir},
    )


def _registrations() -> list[tuple[str, str, str]]:
    """`(event, matcher, script name)` for every hook `settings.json` runs."""
    found: list[tuple[str, str, str]] = []
    for event, entries in _settings()["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                for name in _SCRIPT_IN_A_COMMAND.findall(hook["command"]):
                    found.append((event, entry["matcher"], name))
    return found


def _hook_scripts() -> list[str]:
    """The scripts in `.claude/hooks/` that are hooks. A leading underscore
    marks a module the hooks share rather than one Claude Code runs."""
    return sorted(p.name for p in _HOOKS_DIR.glob("*.py") if not p.name.startswith("_"))


def _declared(script: str) -> tuple[str, str]:
    """`(event, matcher)` from a hook's own docstring."""
    tree = ast.parse((_HOOKS_DIR / script).read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree) or ""
    match = _DECLARED_EVENT.match(docstring.strip())
    assert match is not None, f"{script} does not open with `Event(Matcher) hook`"
    return match.group(1), match.group(2)


class RegistrationTest(unittest.TestCase):
    def test_every_hook_script_is_registered(self):
        registered = {name for _, _, name in _registrations()}
        self.assertEqual(sorted(registered), _hook_scripts())

    def test_every_registration_names_a_script_that_is_there(self):
        for _, _, name in _registrations():
            with self.subTest(hook=name):
                self.assertTrue((_HOOKS_DIR / name).is_file(), name)

    def test_no_hook_is_registered_twice(self):
        names = [name for _, _, name in _registrations()]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_a_shared_module_is_not_registered_as_a_hook(self):
        # `_shell.py` is a library the hooks import. Registered, it would run
        # as a hook, decide nothing, and look like a guard that is in place.
        registered = {name for _, _, name in _registrations()}
        shared = {p.name for p in _HOOKS_DIR.glob("_*.py")}
        self.assertEqual(registered & shared, set())

    def test_each_hook_is_registered_against_the_tool_it_declares(self):
        for event, matcher, name in _registrations():
            with self.subTest(hook=name):
                self.assertEqual((event, matcher), _declared(name))


class MissingScriptTest(unittest.TestCase):
    """A hook script that is not at `$CLAUDE_PROJECT_DIR` turns its hook off,
    and does not stop the session.

    `$CLAUDE_PROJECT_DIR` is not always this checkout. A worktree session can
    be running with the variable pointing at the primary checkout, on another
    branch, which does not carry the scripts this branch adds — and the
    variable's value has changed mid-session, so a session can arrive in that
    state without moving. `python3` on a file that is not there exits 2, and
    a `PreToolUse` hook exiting non-zero blocks the tool call. With the `Bash`
    and `Edit|Write|NotebookEdit` matchers both refusing, the session can
    neither run a command nor change a file, and the way out is to copy the
    scripts into the other checkout by hand from a shell outside the session.

    So the direction here is the opposite of the usual one: an absent guard
    that lets work continue costs less than a present guard that stops all of
    it, because a session that cannot run a command cannot fix anything
    either — including this. The scope is exactly a missing file. A hook that
    is there and crashes still exits non-zero and still blocks, because that
    is a broken guard rather than an absent one, and `… || true` would hide
    it.
    """

    def setUp(self):
        self.empty = tempfile.mkdtemp()

    def test_every_registration_is_the_guarded_form(self):
        expected = sorted(_guarded(script) for script in _hook_scripts())
        self.assertEqual(sorted(_commands()), expected)

    @unittest.skipUnless(_POSIX, "the registrations are run by a POSIX shell")
    def test_a_missing_script_says_nothing_instead_of_blocking(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        for command in _commands():
            with self.subTest(command=command):
                done = _run(command, self.empty, payload)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    @unittest.skipUnless(_POSIX, "the registrations are run by a POSIX shell")
    def test_a_script_that_is_there_still_decides(self):
        # The guard must not swallow the hook it guards: the deny this hook
        # writes to stdout has to come back through the registration as
        # written in settings.json.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "uv pip install x"}})
        done = _run(_command_for("redirect-to-uv.py"), str(_REPO_ROOT), payload)
        self.assertEqual(done.returncode, 0)
        decision = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    @unittest.skipUnless(_POSIX, "the registrations are run by a POSIX shell")
    def test_a_script_that_crashes_stays_loud(self):
        # `… || true` would be shorter than the guard and would also turn a
        # broken hook into a silently absent one.
        broken = Path(self.empty) / ".claude" / "hooks"
        broken.mkdir(parents=True)
        for script in _hook_scripts():
            (broken / script).write_text("import no_such_module\n", encoding="utf-8")
        for command in _commands():
            with self.subTest(command=command):
                done = _run(command, self.empty, "{}")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("no_such_module", done.stderr)


if __name__ == "__main__":
    unittest.main()
