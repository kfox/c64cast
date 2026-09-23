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
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO_ROOT / ".claude" / "hooks"
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"

# The path a registration names, wherever it sits in the shell command that
# runs it — the command mentions it as a variable assignment, as an argument,
# or both.
_SCRIPT_IN_A_COMMAND = re.compile(r"\.claude/hooks/([A-Za-z0-9_.-]+\.py)")

# What a hook's first docstring line declares it is for, e.g.
# `PreToolUse(Edit|Write|NotebookEdit) hook — keep edits out of …`.
_DECLARED_EVENT = re.compile(r"^(\w+)\(([^)]*)\)\s+hook\b")


def _settings() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


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


if __name__ == "__main__":
    unittest.main()
