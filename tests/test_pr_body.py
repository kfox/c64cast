"""Tests for the PR-description checker and the hook that runs it before `gh`."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load(_REPO_ROOT / "scripts" / "check_pr_body.py", "check_pr_body")
hook = _load(_REPO_ROOT / ".claude" / "hooks" / "check-pr-body.py", "check_pr_body_hook")

GOOD = """\
## What changed and why

The flip tracker clears its armed commit on teardown.

## What to look at

The reset in `_page_flip_handler`.

## How it was verified

`make preflight` is green. Deleting the reset fails the new test.
"""


def _with(extra: str) -> str:
    return GOOD + "\n" + extra + "\n"


class TemplateTest(unittest.TestCase):
    def test_the_committed_template_names_exactly_the_checked_sections(self):
        text = (_REPO_ROOT / checker.TEMPLATE).read_text(encoding="utf-8")
        headings = [
            line.removeprefix("## ") for line in text.splitlines() if line.startswith("## ")
        ]
        self.assertEqual(tuple(headings), checker.SECTIONS)

    def test_the_unfilled_template_is_refused_as_empty(self):
        text = (_REPO_ROOT / checker.TEMPLATE).read_text(encoding="utf-8")
        found = checker.problems(text)
        self.assertIn("the 'What changed and why' section is empty", found)
        self.assertIn("the 'How it was verified' section is empty", found)


class SectionsTest(unittest.TestCase):
    def test_a_filled_template_passes(self):
        self.assertEqual(checker.problems(GOOD), [])

    def test_the_optional_section_can_be_left_out(self):
        body = GOOD.replace("## What to look at\n\nThe reset in `_page_flip_handler`.\n\n", "")
        self.assertEqual(checker.problems(body), [])

    def test_a_heading_the_template_does_not_declare_is_refused(self):
        found = checker.problems(_with("## Testing notes\n\nSomething."))
        self.assertEqual(
            found, ["line 13: heading 'Testing notes' is not one the template declares"]
        )

    def test_a_bold_line_standing_in_for_a_heading_is_refused(self):
        found = checker.problems(_with("**Housekeeping:**\n\n- delete wip branches"))
        self.assertEqual(
            found, ["line 13: heading 'Housekeeping' is not one the template declares"]
        )

    def test_a_body_with_no_sections_is_refused(self):
        found = checker.problems("Fixes a thing.\n")
        self.assertEqual(
            found,
            [
                "missing the 'What changed and why' section",
                "missing the 'How it was verified' section",
            ],
        )

    def test_a_repeated_section_is_refused(self):
        found = checker.problems(_with("## How it was verified\n\nAgain."))
        self.assertEqual(found, ["line 13: heading 'How it was verified' appears twice"])

    def test_a_shell_comment_inside_a_code_block_is_not_a_heading(self):
        self.assertEqual(checker.problems(_with("```bash\n# run it\nmake test\n```")), [])

    def test_an_underlined_heading_the_template_does_not_declare_is_refused(self):
        for underline in ("-------", "======="):
            with self.subTest(underline=underline):
                found = checker.problems(_with(f"Caveats\n{underline}\n\nSomething."))
                self.assertEqual(
                    found, ["line 13: heading 'Caveats' is not one the template declares"]
                )

    def test_an_underlined_template_heading_is_a_section(self):
        for heading in ("How it was verified", "How it was verified:"):
            with self.subTest(heading=heading):
                body = GOOD.replace("## How it was verified", f"{heading}\n-------------------")
                self.assertEqual(checker.problems(body), [])

    def test_an_underlined_template_heading_with_nothing_under_it_is_empty(self):
        body = GOOD.split("## How it was verified")[0] + "How it was verified\n---\n"
        self.assertEqual(checker.problems(body), ["the 'How it was verified' section is empty"])

    def test_a_rule_after_a_blank_line_a_rule_or_a_list_item_is_not_a_heading(self):
        self.assertEqual(checker.problems(_with("---\n---\n\n- one item\n---")), [])

    def test_a_rule_inside_a_code_block_is_not_an_underline(self):
        for block in ("```yaml\nkey: value\n---\n```", "```\nmake test\n```\n---"):
            with self.subTest(block=block):
                self.assertEqual(checker.problems(_with(block)), [])

    def test_template_hints_left_in_html_comments_do_not_count(self):
        body = _with("<!--\n## Follow-ups\nnot in this PR\n-->")
        self.assertEqual(checker.problems(body), [])


class ContentTest(unittest.TestCase):
    def assertRefused(self, text: str, fragment: str):
        found = checker.problems(_with(text))
        self.assertTrue(any(fragment in item for item in found), found)

    def test_attribution_is_refused(self):
        self.assertRefused("Co-Authored-By: someone", "'Co-Authored-By'")
        self.assertRefused("Written with Claude.", "'Claude'")
        self.assertRefused("https://claude.ai/code/session_x", "'claude.ai'")
        self.assertRefused("\N{ROBOT FACE} Generated with a tool", "'\N{ROBOT FACE}'")
        self.assertRefused("Generated with [a tool](https://example.com)", "'Generated with ['")

    def test_a_file_generated_with_a_make_target_is_not_attribution(self):
        self.assertEqual(
            checker.problems(_with("`schema.json` is generated with `make schema`.")), []
        )

    def test_the_agent_config_directory_and_instructions_file_are_not_attribution(self):
        self.assertEqual(
            checker.problems(_with("Adds `.claude/hooks/x.py` and edits CLAUDE.md.")), []
        )

    def test_machine_specifics_are_refused(self):
        self.assertRefused("Ran against 192.168.2.64.", "LAN address '192.168.2.64'")
        self.assertRefused("Captured with the Cam Link.", "'Cam Link'")
        self.assertRefused("See /Users/someone/scratch/out.png.", "'/Users/someone'")

    def test_loopback_public_and_version_numbers_are_not_machine_specifics(self):
        body = _with("Binds 127.0.0.1 or 0.0.0.0; firmware 3.15.0.1; DNS 8.8.8.8.")
        self.assertEqual(checker.problems(body), [])

    def test_deferred_work_and_review_narrative_are_refused(self):
        for text, fragment in (
            ("Follow-ups: tidy the parser.", "'Follow-ups'"),
            ("Not in this PR: the TR path.", "'Not in this PR'"),
            ("The rest is out of scope.", "'out of scope'"),
            ("Two findings declined during review.", "'declined'"),
            ("Pending hardware re-test.", "'Pending hardware'"),
            ("All scenes play; machine reset afterward.", "'reset afterward'"),
        ):
            with self.subTest(text=text):
                self.assertRefused(text, fragment)


class CheckerMainTest(unittest.TestCase):
    def test_a_refused_body_exits_one_and_says_why(self):
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("nothing\n")), contextlib.redirect_stderr(stderr):
            self.assertEqual(checker.main(["check_pr_body.py", "-"]), 1)
        self.assertIn("PR description refused", stderr.getvalue())
        self.assertIn("missing the 'What changed and why' section", stderr.getvalue())

    def test_a_good_body_file_exits_zero(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "body.md"
        path.write_text(GOOD, encoding="utf-8")
        self.assertEqual(checker.main(["check_pr_body.py", str(path)]), 0)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def _file(self, body: str) -> str:
        path = Path(self.directory) / "body.md"
        path.write_text(body, encoding="utf-8")
        return "body.md"

    def test_a_good_body_file_passes(self):
        name = self._file(GOOD)
        self.assertIsNone(
            hook.decide(f"gh pr create --draft --title t --body-file {name}", self.directory)
        )

    def test_a_bad_body_file_is_denied_with_the_checker_report(self):
        name = self._file(_with("## Follow-ups"))
        reason = hook.decide(f"gh pr create --draft -F {name}", self.directory)
        self.assertIn("heading 'Follow-ups' is not one the template declares", reason)

    def test_an_inline_body_is_checked(self):
        cmd = "gh pr edit 12 --body='Fixes a thing.'"
        self.assertIn(
            "missing the 'What changed and why' section", hook.decide(cmd, self.directory)
        )

    def test_a_heredoc_body_is_checked(self):
        cmd = f"gh pr create --title t --body-file - <<'EOF'\n{GOOD}EOF"
        self.assertIsNone(hook.decide(cmd, self.directory))
        cmd = "gh pr create --title t --body-file - <<'EOF'\nFixes a thing.\nEOF"
        self.assertIn(
            "missing the 'What changed and why' section", hook.decide(cmd, self.directory)
        )

    def test_a_body_built_by_substitution_is_denied_as_unreadable(self):
        cmd = 'gh pr create --title t --body "$(cat body.md)"'
        self.assertEqual(hook.decide(cmd, self.directory), hook.USE_BODY_FILE)

    def test_create_without_a_body_is_denied(self):
        self.assertEqual(
            hook.decide("gh pr create --fill --draft", self.directory), hook.USE_BODY_FILE
        )

    def test_a_missing_body_file_is_denied(self):
        self.assertEqual(hook.decide("gh pr create -F nope.md", self.directory), hook.USE_BODY_FILE)

    def test_an_edit_that_leaves_the_body_alone_passes(self):
        self.assertIsNone(hook.decide("gh pr edit 12 --add-label bug", self.directory))

    def test_a_command_after_a_cd_and_env_prefix_is_still_read(self):
        cmd = "cd /x && GH_HOST=github.com gh pr create --body 'nothing'"
        self.assertIsNotNone(hook.decide(cmd, self.directory))

    def test_a_body_file_is_read_from_the_directory_a_cd_moved_to(self):
        self._file(GOOD)
        moved_to = tempfile.mkdtemp()
        (Path(moved_to) / "body.md").write_text(_with("## Follow-ups"), encoding="utf-8")
        reason = hook.decide(f"cd {moved_to} && gh pr create -F body.md", self.directory)
        self.assertIn("heading 'Follow-ups' is not one the template declares", reason)

    def test_a_body_file_under_the_home_directory_is_read(self):
        self._file(GOOD)
        with mock.patch.dict("os.environ", {"HOME": self.directory}):
            self.assertIsNone(hook.decide("gh pr create -F ~/body.md", self.directory))

    def test_the_new_alias_of_create_is_checked(self):
        self.assertIn(
            "missing the 'What changed and why' section",
            hook.decide("gh pr new --body 'Fixes a thing.'", self.directory),
        )

    def test_other_gh_commands_pass(self):
        self.assertIsNone(hook.decide("gh pr view 12 --json body", self.directory))
        self.assertIsNone(hook.decide("gh issue create --body 'Follow-ups'", self.directory))

    def test_main_prints_a_deny_decision(self):
        payload = json.dumps(
            {"tool_input": {"command": "gh pr create --fill"}, "cwd": self.directory}
        )
        stdout = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(stdout):
            self.assertEqual(hook.main(), 0)
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], hook.USE_BODY_FILE)


if __name__ == "__main__":
    unittest.main()
