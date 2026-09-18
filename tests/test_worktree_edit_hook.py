"""Tests for `.claude/hooks/require-edits-in-a-worktree.py` — the PreToolUse
hook that asks before an edit lands in the repository's primary checkout.

The decision is `ask`, so both failure directions cost the user a prompt rather
than a lost edit. The cases that matter are the ones where the hook has to work
out which tree a path belongs to: a session inside a worktree reaching back
into the shared tree, and a path outside the repository entirely.

The hook is loaded by path: `.claude/hooks/` is not a package, and the filename
is not an identifier.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = _REPO_ROOT / ".claude" / "hooks" / "require-edits-in-a-worktree.py"


def _clone(root: Path) -> Path:
    """`root` made to look like a clone: `.git` is a directory."""
    (root / ".git").mkdir(parents=True)
    return root


def _linked_worktree(root: Path, clone: Path) -> Path:
    """`root` made to look like a worktree linked from `clone`, which is what
    `git worktree add` writes."""
    return _worktree_recording(root, clone / ".git" / "worktrees" / root.name)


def _worktree_recording(root: Path, admin: Path | str) -> Path:
    """`root` made to look like a worktree whose `.git` file records `admin`
    verbatim — the value git writes there is not always an absolute real
    path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return root


# A real tree, because the hook settles which checkout a path belongs to by
# looking for `.git`. Left for the OS to reap.
_TREE = Path(tempfile.mkdtemp()).resolve()
_MAIN = _clone(_TREE / "c64cast")
_WORKTREE = _linked_worktree(_MAIN / ".claude" / "worktrees" / "ship-clean", _MAIN)
_STRAY = _linked_worktree(_TREE / "c64cast-386", _MAIN)
_LOOSE = _TREE / "not-a-checkout"
_LOOSE.mkdir()


def _load_hook():
    spec = importlib.util.spec_from_file_location("require_edits_in_a_worktree", _HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


class AllowedPathTest(unittest.TestCase):
    def assertAllowed(self, path: Path | str, cwd: Path, project_dir: Path) -> None:
        self.assertIsNone(hook.verdict(str(path), str(cwd), str(project_dir)), str(path))

    def test_a_path_in_the_sessions_own_worktree_passes(self):
        self.assertAllowed(_WORKTREE / "tests" / "x.py", _WORKTREE, _WORKTREE)

    def test_a_relative_path_in_the_worktree_passes(self):
        self.assertAllowed("tests/x.py", _WORKTREE, _WORKTREE)

    def test_a_path_in_another_worktree_of_the_same_repo_passes(self):
        other = _linked_worktree(_MAIN / ".claude" / "worktrees" / "second", _MAIN)
        self.assertAllowed(other / "tests" / "x.py", _WORKTREE, _WORKTREE)

    def test_a_worktree_outside_the_checkout_passes(self):
        # Already a worktree; where a new one may go is the other hook's call.
        self.assertAllowed(_STRAY / "tests" / "x.py", _STRAY, _STRAY)

    def test_a_path_in_no_checkout_at_all_passes(self):
        self.assertAllowed(_LOOSE / "notes.md", _LOOSE, _LOOSE)

    def test_a_path_outside_the_repository_passes_from_inside_it(self):
        # A scratchpad file, or the user's own dotfiles.
        self.assertAllowed(_LOOSE / "probe.py", _WORKTREE, _WORKTREE)


class RefusedPathTest(unittest.TestCase):
    def assertAsked(self, path: Path | str, cwd: Path, project_dir: Path) -> str:
        reason = hook.verdict(str(path), str(cwd), str(project_dir))
        self.assertIsNotNone(reason, str(path))
        assert reason is not None
        return reason

    def test_a_path_in_the_primary_checkout_is_asked_about(self):
        self.assertAsked(_MAIN / "c64cast" / "app" / "cli.py", _MAIN, _MAIN)

    def test_a_relative_path_resolves_against_the_working_directory(self):
        self.assertAsked("c64cast/app/cli.py", _MAIN, _MAIN)

    def test_a_worktree_session_reaching_back_into_the_shared_tree_is_asked_about(self):
        # The case this exists for: the session is isolated, the edit is not.
        self.assertAsked(_MAIN / "Makefile", _WORKTREE, _WORKTREE)

    def test_the_shared_trees_own_claude_directory_is_asked_about(self):
        self.assertAsked(_MAIN / ".claude" / "settings.json", _WORKTREE, _WORKTREE)

    def test_a_path_that_only_starts_to_look_like_a_worktree_is_asked_about(self):
        self.assertAsked(_MAIN / ".claude" / "hooks" / "x.py", _MAIN, _MAIN)
        self.assertAsked(_MAIN / "docs" / ".claude" / "worktrees" / "x.py", _MAIN, _MAIN)

    def test_a_worktree_recording_a_relative_gitdir_still_sees_the_shared_tree(self):
        # The path git records is not always absolute, and a hook that reads it
        # literally resolves the checkout to `../../..` and then matches
        # nothing, which fails open.
        relative = _worktree_recording(
            _MAIN / ".claude" / "worktrees" / "relative-verdict",
            Path("..") / ".." / ".." / ".git" / "worktrees" / "relative-verdict",
        )
        self.assertAsked(_MAIN / "Makefile", relative, relative)

    def test_the_reason_names_the_path_and_how_to_get_a_worktree(self):
        reason = self.assertAsked(_MAIN / "Makefile", _MAIN, _MAIN)
        self.assertIn(str(_MAIN / "Makefile"), reason)
        self.assertIn("EnterWorktree", reason)
        # make takes the override as an argument; git needs it as a prefix.
        self.assertIn("make <target> UV_PROJECT_ENVIRONMENT=<worktree>/.venv", reason)
        self.assertIn("UV_PROJECT_ENVIRONMENT=<worktree>/.venv git commit", reason)

    def test_the_reason_given_inside_a_worktree_does_not_offer_enterworktree(self):
        # EnterWorktree refuses a `name` from a session already in a worktree,
        # so offering it there costs a tool call and answers nothing.
        reason = self.assertAsked(_MAIN / "Makefile", _WORKTREE, _WORKTREE)
        self.assertNotIn("EnterWorktree", reason)
        self.assertIn(str(_WORKTREE), reason)

    def test_a_worktree_outside_the_checkout_is_told_where_it_is(self):
        # EnterWorktree refuses a `name` from a session in any linked worktree,
        # so its location does not change what the message can offer.
        reason = self.assertAsked(_MAIN / "Makefile", _STRAY, _STRAY)
        self.assertNotIn("EnterWorktree", reason)
        self.assertIn(str(_STRAY), reason)

    def test_a_working_directory_in_a_worktree_settles_the_reason(self):
        # The two can disagree: a session started in the shared tree and then
        # moved into a worktree, where EnterWorktree is refused all the same.
        reason = self.assertAsked(_MAIN / "Makefile", _WORKTREE, _MAIN)
        self.assertNotIn("EnterWorktree", reason)
        self.assertIn(str(_WORKTREE), reason)


class CheckoutResolutionTest(unittest.TestCase):
    """Which tree the hook takes to be primary, given where it was started."""

    def test_a_worktree_and_the_clone_resolve_to_the_same_checkout(self):
        self.assertEqual(hook._primary_checkout(_WORKTREE), _MAIN)
        self.assertEqual(hook._primary_checkout(_MAIN), _MAIN)

    def test_a_directory_below_either_resolves_upward(self):
        self.assertEqual(hook._primary_checkout(_MAIN / "c64cast" / "app"), _MAIN)
        self.assertEqual(hook._primary_checkout(_WORKTREE / "tests"), _MAIN)

    def test_a_directory_in_no_checkout_resolves_to_nothing(self):
        self.assertIsNone(hook._primary_checkout(_LOOSE))

    def test_a_git_file_that_names_nothing_usable_resolves_to_nothing(self):
        broken = _TREE / "broken"
        broken.mkdir()
        (broken / ".git").write_text("not a gitdir line\n", encoding="utf-8")
        self.assertIsNone(hook._primary_checkout(broken))

    def test_a_relative_gitdir_resolves_against_the_worktree(self):
        # `worktree.useRelativePaths`, `--relative-paths`, and some
        # `git worktree repair` runs write the value this way.
        relative = _worktree_recording(
            _MAIN / ".claude" / "worktrees" / "relative",
            Path("..") / ".." / ".." / ".git" / "worktrees" / "relative",
        )
        self.assertEqual(hook._primary_checkout(relative), _MAIN)

    def test_a_gitdir_through_a_symlink_resolves_to_the_same_checkout(self):
        link = _TREE / "link-to-main"
        try:
            link.symlink_to(_MAIN, target_is_directory=True)
        except OSError:
            self.skipTest("this platform will not let the test create a directory symlink")
        through = _worktree_recording(
            _MAIN / ".claude" / "worktrees" / "symlinked",
            link / ".git" / "worktrees" / "symlinked",
        )
        self.assertEqual(hook._primary_checkout(through), _MAIN)


class HookProtocolTest(unittest.TestCase):
    def _main(self, payload: str, project_dir: Path = _MAIN) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(project_dir)}):
                code = hook.main()
        return code, out.getvalue()

    def _payload(self, tool_input: dict[str, str], cwd: Path = _MAIN) -> str:
        return json.dumps({"tool_name": "Edit", "tool_input": tool_input, "cwd": str(cwd)})

    def test_a_shared_tree_edit_prints_an_ask_decision(self):
        target = _MAIN / "Makefile"
        code, printed = self._main(self._payload({"file_path": str(target)}))
        self.assertEqual(code, 0)
        decision = json.loads(printed)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn(str(target), decision["permissionDecisionReason"])

    def test_a_notebook_is_read_from_its_own_key(self):
        target = _MAIN / "notes.ipynb"
        _, printed = self._main(self._payload({"notebook_path": str(target)}))
        self.assertIn(str(target), printed)

    def test_a_worktree_edit_prints_nothing(self):
        target = _WORKTREE / "tests" / "x.py"
        payload = self._payload({"file_path": str(target)}, cwd=_WORKTREE)
        self.assertEqual(self._main(payload, project_dir=_WORKTREE), (0, ""))

    def test_a_payload_that_is_not_json_is_allowed_rather_than_blocked(self):
        self.assertEqual(self._main("not json at all"), (0, ""))

    def test_a_payload_with_no_path_is_allowed(self):
        self.assertEqual(self._main(json.dumps({"tool_input": {}})), (0, ""))


if __name__ == "__main__":
    unittest.main()
