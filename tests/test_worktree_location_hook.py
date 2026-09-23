"""Tests for `.claude/hooks/require-worktrees-in-checkout.py` — the PreToolUse hook
that keeps a git worktree under the checkout's `.claude/worktrees/`.

The two failure directions are not symmetric. A false deny prints the
destination it refused and the form to use instead, so it costs a round trip;
a false allow is silent, and the stray worktree surfaces later as whatever
walks the tree reading another branch's files as this one's. The cases here
are weighted accordingly.

The hook is loaded by path: `.claude/hooks/` is not a package, and the
filename is not an identifier.
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
_HOOK_PATH = _REPO_ROOT / ".claude" / "hooks" / "require-worktrees-in-checkout.py"


def _clone(root: Path) -> Path:
    """`root` made to look to the hook like a clone: `.git` is a directory."""
    (root / ".git").mkdir(parents=True)
    return root


def _in_a_command(path: Path | str) -> str:
    """`path` spelled for a command string the hook will lex.

    The lexer is `shlex(posix=True)`, where a backslash escapes the character
    after it, so a native Windows path interpolated into a command arrives
    with its separators eaten. `as_posix()` is what `Path` accepts on either
    platform, and a no-op off Windows.
    """
    return Path(path).as_posix()


def _linked_worktree(root: Path) -> Path:
    """`root` made to look like a linked worktree: `.git` is a file naming the
    admin directory, which is what `git worktree add` writes."""
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    return root


# A real tree: the destination's `.claude/worktrees` has to belong to a
# checkout, which the hook settles by looking for a `.git`. Left for the OS to
# reap. A refused destination needs no tree and stays a fake absolute path.
_TREE = Path(tempfile.mkdtemp())
_HOME_DIR = _TREE / "home"
_HOME_DIR.mkdir()
_HOME = str(_HOME_DIR.resolve())
_CWD = str(_clone(_TREE / "checkout").resolve())
_ALLOWED = f"{_in_a_command(_CWD)}/.claude/worktrees"

# Both homes, because `expanduser` reads HOME on POSIX and USERPROFILE on
# Windows, and the hook expands `~` and compares against `Path.home()` — a
# fake that moved only one of them would answer inconsistently and pass a `~`
# path. CLAUDE_PROJECT_DIR because the hook names the sanctioned location
# relative to it, falling back to the working directory when it is unset.
_PINNED_ENV = {"HOME": _HOME, "USERPROFILE": _HOME, "CLAUDE_PROJECT_DIR": _CWD}


def _in_a_reason(path: str) -> str:
    """`path` spelled the way a refusal's reason spells it — resolved against
    `_CWD`.

    A POSIX-looking absolute path is drive-relative on Windows, so
    `/private/tmp/x` is reported there as `C:\\private\\tmp\\x`.
    """
    return str((Path(_CWD) / path).resolve())


def _load_hook():
    spec = importlib.util.spec_from_file_location("require_worktrees_in_checkout", _HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def _verdict(cmd: str, cwd: str = _CWD) -> str | None:
    with mock.patch.dict(os.environ, _PINNED_ENV):
        return hook.verdict(cmd, cwd)


class AllowedDestinationTest(unittest.TestCase):
    def assertAllowed(self, cmd: str, cwd: str = _CWD) -> None:
        self.assertIsNone(_verdict(cmd, cwd), cmd)

    def test_the_default_location_passes_relative_and_absolute(self):
        self.assertAllowed("git worktree add -b feat/x .claude/worktrees/x origin/main")
        self.assertAllowed(f"git worktree add {_ALLOWED}/x")

    def test_a_name_with_path_segments_in_it_passes(self):
        # EnterWorktree accepts `feat/x` as a name, which nests two deep.
        self.assertAllowed("git worktree add .claude/worktrees/feat/x")

    def test_a_cd_prefix_and_not_the_shells_directory_places_the_destination(self):
        moved = _in_a_command(_CWD)
        self.assertAllowed(f"cd {moved} && git worktree add .claude/worktrees/x", cwd="/elsewhere")
        self.assertAllowed(f"cd {moved}/.claude && git worktree add worktrees/x", cwd="/elsewhere")

    def test_a_line_continuation_is_read_as_one_command(self):
        self.assertAllowed("git worktree add -b feat/x \\\n  .claude/worktrees/x origin/main")

    def test_a_git_worktree_subcommand_that_creates_nothing_passes(self):
        self.assertAllowed("git worktree list")
        self.assertAllowed("git worktree remove --force /private/tmp/stray")
        self.assertAllowed("git worktree prune")
        self.assertAllowed("git worktree add -h")
        self.assertAllowed("git worktree add --help")

    def test_a_clustered_short_flag_that_carries_its_value_passes(self):
        # `-bfeat/x` holds the branch inside the token; `-fb feat/x` takes the
        # token after it. Either way the destination is the last one.
        self.assertAllowed("git worktree add -fb feat/x .claude/worktrees/x")
        self.assertAllowed("git worktree add -bfeat/x .claude/worktrees/x")

    def test_a_command_that_is_not_git_worktree_passes(self):
        self.assertAllowed("make test")
        self.assertAllowed("git commit -m 'worktree add notes'")

    def test_a_quoted_string_spanning_lines_is_read_rather_than_refused(self):
        # A commit message with a body is the everyday form, and refusing a
        # command the lexer choked on would refuse all of them.
        self.assertAllowed('git commit -m "subject\n\nbody"')
        self.assertAllowed("echo 'an unterminated quote and no worktree in sight")

    def test_a_popd_returns_to_where_its_pushd_started(self):
        self.assertAllowed("pushd ~ && popd && git worktree add .claude/worktrees/x")

    def test_prose_that_only_names_the_command_passes(self):
        # A review record is written through a heredoc, and quotes the
        # destinations it probed.
        self.assertAllowed(
            "record 076cb94 <<'REPORT'\ngit worktree add /private/tmp/probed\nREPORT"
        )
        self.assertAllowed("echo 'git worktree add /private/tmp/x'")
        self.assertAllowed("grep -rn 'x' docs/ # git worktree add /private/tmp/x")

    def test_a_redirect_before_the_destination_still_reads_it(self):
        # The `2` shlex leaves in front of `>` is the file descriptor, not a
        # positional, so keeping it refuses a destination that is in the right
        # place and mis-reads the one that is not.
        self.assertAllowed("git worktree add 2>/dev/null .claude/worktrees/x")
        self.assertAllowed(f"git worktree move 2>/dev/null /private/tmp/a {_ALLOWED}/b")

    def test_prose_that_names_the_subcommand_without_naming_git_passes(self):
        # Unquoted, so the words arrive as tokens of the segment rather than
        # inside one. Some token has to be git for this to be a command.
        self.assertAllowed("echo worktree add /private/tmp/x")
        self.assertAllowed("grep -rn worktree add docs/")


class RefusedDestinationTest(unittest.TestCase):
    def assertRefused(self, cmd: str, cwd: str = _CWD) -> str:
        reason = _verdict(cmd, cwd)
        self.assertIsNotNone(reason, cmd)
        assert reason is not None
        return reason

    def test_an_absolute_path_outside_the_checkout_is_refused(self):
        self.assertRefused("git worktree add /private/tmp/c64cast-386 -b pr-386")

    def test_a_sibling_of_the_checkout_is_refused(self):
        self.assertRefused("git worktree add -b pr-385 ../c64cast-wt-407")

    def test_an_unexpanded_command_substitution_is_refused(self):
        # `$(mktemp -d)/name` is the shape that put the strays in `$TMPDIR`.
        self.assertRefused('git worktree add "$(mktemp -d)/c64cast-433"')

    def test_the_home_directorys_own_claude_worktrees_is_refused(self):
        # `~` and `$HOME` both, since the shell expands them and shlex does not.
        self.assertRefused("git worktree add ~/.claude/worktrees/mergequeue")
        self.assertRefused("git worktree add $HOME/.claude/worktrees/mergequeue")

    def test_a_branch_name_is_not_mistaken_for_the_destination(self):
        # -b consumes the next token, so the decision is about /private/tmp/x.
        self.assertRefused("git worktree add -b .claude/worktrees/decoy /private/tmp/x")

    def test_a_clustered_short_flag_does_not_hide_the_destination(self):
        # `-fb x` is `-f -b x`: git reads -b's value from the next token, so
        # the destination is the one after that.
        self.assertRefused("git worktree add -fb .claude/worktrees/decoy /private/tmp/x")
        self.assertRefused("git worktree add -fB .claude/worktrees/decoy /private/tmp/x")

    def test_moving_a_worktree_out_of_the_directory_is_refused(self):
        self.assertRefused("git worktree move .claude/worktrees/x /private/tmp/elsewhere")

    def test_a_cd_into_a_stray_directory_is_refused(self):
        self.assertRefused("cd /private/tmp && git worktree add c64cast-xyz")

    def test_a_cd_to_the_home_directory_takes_the_destination_with_it(self):
        self.assertRefused("cd ~ && git worktree add .claude/worktrees/mergequeue")
        self.assertRefused("cd && git worktree add .claude/worktrees/mergequeue")

    def test_a_git_C_moves_where_a_relative_destination_lands(self):
        self.assertRefused(
            f"git -C {_in_a_command(_HOME)} worktree add .claude/worktrees/mergequeue"
        )
        self.assertRefused("git -C ~ worktree add .claude/worktrees/mergequeue")

    def test_a_separator_the_shell_needs_no_space_around_still_splits(self):
        self.assertRefused("echo hi; git worktree add /private/tmp/x")
        self.assertRefused("echo hi|git worktree add /private/tmp/x")
        self.assertRefused("(git worktree add /private/tmp/x)")

    def test_a_later_line_of_a_multi_line_command_is_read(self):
        self.assertRefused("set -e\ngit worktree add /private/tmp/x")
        self.assertRefused("cat <<'EOF' > f.md\nprose\nEOF\ngit worktree add /private/tmp/x")

    def test_a_continuation_before_the_destination_is_read(self):
        self.assertRefused("git worktree add -b feat/x \\\n  /private/tmp/x")

    def test_a_command_handed_to_another_shell_is_read(self):
        self.assertRefused("bash -c 'git worktree add /private/tmp/x'")
        self.assertRefused('sh -c "cd ~ && git worktree add .claude/worktrees/mergequeue"')
        self.assertRefused('eval "git worktree add /private/tmp/x"')

    def test_a_heredoc_read_by_a_shell_is_a_script_rather_than_prose(self):
        # A heredoc body is skipped because a review record is written
        # through one. When the reader is a shell the body is not prose at
        # all, it is the script that shell runs — the same bypass as
        # `bash -c`, written the other way round.
        self.assertRefused("bash <<'EOF'\ngit worktree add /private/tmp/x\nEOF")
        self.assertRefused("bash <<EOF\ngit worktree add /private/tmp/x\nEOF")
        self.assertRefused("sh <<'EOF'\ncd ~\ngit worktree add .claude/worktrees/x\nEOF")
        self.assertRefused("env bash <<'EOF'\ngit worktree add /private/tmp/x\nEOF")

    def test_a_wrapper_in_front_of_the_shell_does_not_hide_the_payload(self):
        # The shell is looked for anywhere in the segment, and its `-c` may
        # arrive clustered with other short flags.
        self.assertRefused("env bash -c 'git worktree add /private/tmp/x'")
        self.assertRefused("nohup bash -c 'git worktree add /private/tmp/x'")
        self.assertRefused("bash -lc 'git worktree add /private/tmp/x'")

    def test_a_heredoc_body_ending_in_a_backslash_does_not_eat_its_terminator(self):
        # A review record's prose wraps; joining the continuation would fuse
        # the last body line with REPORT and swallow every line after it.
        self.assertRefused(
            "record abc123 <<'REPORT'\nprose that wraps \\\nREPORT\ngit worktree add /private/tmp/x"
        )

    def test_a_quote_that_spans_lines_does_not_hide_the_command_behind_it(self):
        # Lexing line by line leaves every line of these unlexable, and an
        # unlexable line dropped is the whole command waved through.
        self.assertRefused("bash -c 'set -e\ngit worktree add /private/tmp/x'")
        self.assertRefused('git commit -m "subject\n\nbody" && git worktree add /private/tmp/x')

    def test_an_unterminated_quote_naming_the_command_is_refused(self):
        self.assertRefused("git worktree add '/private/tmp/x")

    def test_a_punctuation_cluster_does_not_hide_the_add_behind_a_cd(self):
        # shlex hands back `;(` and `&&(` whole, so they match no separator and
        # the `cd` and the add arrive in one argv.
        self.assertRefused("cd ~;(git worktree add .claude/worktrees/x)")
        self.assertRefused("cd ~&&(git worktree add .claude/worktrees/x)")

    def test_a_pushd_moves_where_a_relative_destination_lands(self):
        self.assertRefused("pushd ~ && git worktree add .claude/worktrees/mergequeue")

    def test_a_wrapper_around_git_does_not_hide_the_add(self):
        self.assertRefused("/usr/bin/git worktree add /private/tmp/x")
        self.assertRefused("env git worktree add /private/tmp/x")
        self.assertRefused("echo /private/tmp/x | xargs git worktree add")

    def test_a_command_substitution_does_not_hide_the_add(self):
        # shlex splits `$(` off the word after it but leaves a backtick glued
        # on, so the two forms reach the git-naming gate differently.
        self.assertRefused("echo $(git worktree add /private/tmp/x)")
        self.assertRefused("echo `git worktree add /private/tmp/x`")

    def test_a_redirects_file_descriptor_does_not_shift_the_destination(self):
        # shlex splits `2>` into `2` and `>`; the `2` left in the argv becomes
        # `move`'s first positional and pushes the real destination out of the
        # window the hook reads.
        self.assertRefused("git worktree move 2>/dev/null .claude/worktrees/a /private/tmp/b")

    def test_a_global_flag_that_takes_a_value_does_not_hide_the_add(self):
        self.assertRefused("git --namespace ns worktree add /private/tmp/x")
        self.assertRefused("git --exec-path /usr/libexec worktree add /private/tmp/x")

    def test_an_add_with_no_destination_is_refused_rather_than_waved_through(self):
        # Malformed, so git would reject it too — but the safe direction for a
        # destination the hook cannot read is to say so.
        self.assertRefused("git worktree add")

    def test_the_reason_names_the_refused_destination_and_where_to_put_it(self):
        reason = self.assertRefused("git worktree add /private/tmp/c64cast-386")
        self.assertIn(_in_a_reason("/private/tmp/c64cast-386"), reason)
        self.assertIn(_in_a_reason(".claude/worktrees"), reason)

    def test_the_reason_names_a_relative_destination_resolved(self):
        reason = self.assertRefused("git worktree add ../c64cast-wt-407")
        self.assertIn(_in_a_reason("../c64cast-wt-407"), reason)

    def test_an_ambient_project_dir_does_not_move_where_the_reason_points(self):
        # The hook reads CLAUDE_PROJECT_DIR to name the sanctioned location,
        # and Claude Code sets it — so an unpinned suite would assert against
        # whatever ran it.
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/elsewhere"}):
            reason = self.assertRefused("git worktree add /private/tmp/c64cast-386")
        self.assertIn(_in_a_reason(".claude/worktrees"), reason)


class CheckoutMarkerTest(unittest.TestCase):
    """The `.claude/worktrees` has to belong to a checkout.

    Without that, a refusal teaches the way around itself: `mkdir -p
    $(mktemp -d)/.claude/worktrees` satisfies the message's wording and puts
    the worktree right back where the strays were.
    """

    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())

    def _verdict(self, target: Path, home: str) -> str | None:
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            return hook.verdict(f"git worktree add {_in_a_command(target)}", _CWD)

    def assertDestinationAllowed(self, target: Path, home: str = _HOME) -> None:
        self.assertIsNone(self._verdict(target, home), str(target))

    def assertDestinationRefused(self, target: Path, home: str = _HOME) -> None:
        self.assertIsNotNone(self._verdict(target, home), str(target))

    def test_a_clone_and_a_linked_worktree_both_carry_the_marker(self):
        clone = _clone(self.tree / "clone")
        worktree = _linked_worktree(self.tree / "wt")
        self.assertDestinationAllowed(clone / ".claude" / "worktrees" / "x")
        self.assertDestinationAllowed(worktree / ".claude" / "worktrees" / "x")

    def test_a_worktrees_dir_with_no_checkout_above_it_is_refused(self):
        stray = self.tree / "junk" / ".claude" / "worktrees" / "c64cast-433"
        stray.parent.mkdir(parents=True)
        self.assertDestinationRefused(stray)

    def test_the_destinations_own_directories_need_not_exist_yet(self):
        clone = _clone(self.tree / "fresh")
        self.assertFalse((clone / ".claude").exists())
        self.assertDestinationAllowed(clone / ".claude" / "worktrees" / "x")

    def test_only_the_checkout_roots_own_worktrees_dir_counts(self):
        clone = _clone(self.tree / "clone")
        self.assertDestinationRefused(clone / "sub" / ".claude" / "worktrees" / "x")

    def test_a_symlink_to_a_checkout_resolves_to_the_checkout(self):
        clone = _clone(self.tree / "clone")
        link = self.tree / "link"
        try:
            link.symlink_to(clone, target_is_directory=True)
        except OSError:
            self.skipTest("this platform will not let the test create a directory symlink")
        self.assertDestinationAllowed(link / ".claude" / "worktrees" / "x")

    def test_a_home_directory_that_is_itself_a_checkout_is_still_refused(self):
        # Dotfiles-in-git puts a `.git` in `$HOME`, and `~/.claude/worktrees`
        # is still Claude Code's own directory rather than that checkout's.
        home = _clone(self.tree / "dotfiles-home")
        self.assertDestinationRefused(home / ".claude" / "worktrees" / "x", home=str(home))


class HookProtocolTest(unittest.TestCase):
    def _main(self, payload: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
            with mock.patch.dict(os.environ, _PINNED_ENV):
                code = hook.main()
        return code, out.getvalue()

    def _payload(self, cmd: str) -> str:
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": _CWD})

    def test_a_refused_command_prints_a_deny_decision(self):
        code, printed = self._main(self._payload("git worktree add /private/tmp/x"))
        self.assertEqual(code, 0)
        decision = json.loads(printed)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(_in_a_reason("/private/tmp/x"), decision["permissionDecisionReason"])

    def test_an_allowed_command_prints_nothing(self):
        code, printed = self._main(self._payload("git worktree add .claude/worktrees/x"))
        self.assertEqual((code, printed), (0, ""))

    def test_a_payload_that_is_not_json_is_allowed_rather_than_blocked(self):
        self.assertEqual(self._main("not json at all"), (0, ""))

    def test_a_payload_with_no_command_is_allowed(self):
        self.assertEqual(self._main(json.dumps({"tool_input": {}})), (0, ""))


if __name__ == "__main__":
    unittest.main()
