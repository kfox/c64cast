"""The two write-time prose checks: `check_commit_message` and `lint_comments`.

Both refuse things review would otherwise raise as findings, and a prose finding
costs a round that a reword then multiplies across every stacked SHA. The caps
are measured constants, so the tests pin the measured values rather than
whatever the scripts currently say.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SUBJECT_MAX = 80
_BODY_MAX = 10


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


msg = _load_script("check_commit_message")
lint = _load_script("lint_comments")


def _lines(subject: str, body: str = "") -> list[str]:
    return msg.message_lines(f"{subject}\n\n{body}" if body else subject)


def _violations(subject: str, body: str = "") -> list[str]:
    return msg.violations(_lines(subject, body), _SUBJECT_MAX, _BODY_MAX)


def _run_main(raw: str) -> tuple[int, str]:
    """`main` over a message file, with its report captured rather than printed."""
    path = Path(tempfile.mkdtemp()) / "COMMIT_EDITMSG"
    path.write_text(raw, encoding="utf-8")

    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        code = msg.main(["check_commit_message.py", str(path)])

    return code, buffer.getvalue()


class DefaultCapTest(unittest.TestCase):
    def test_the_shipped_defaults_are_the_measured_ones(self) -> None:
        self.assertEqual(msg.DEFAULT_SUBJECT_MAX, _SUBJECT_MAX)
        self.assertEqual(msg.DEFAULT_BODY_MAX, _BODY_MAX)


class MessageShapeTest(unittest.TestCase):
    def test_a_subject_at_the_cap_passes_and_one_over_it_does_not(self) -> None:
        self.assertEqual(_violations("x" * _SUBJECT_MAX), [])

        over = _violations("x" * (_SUBJECT_MAX + 1))
        self.assertEqual(len(over), 1)
        self.assertIn(f"over the {_SUBJECT_MAX} allowed", over[0])

    def test_a_body_at_the_cap_passes_and_one_over_it_does_not(self) -> None:
        within = "\n".join(f"line {n}" for n in range(_BODY_MAX))
        self.assertEqual(_violations("fix: a thing", within), [])

        over = _violations("fix: a thing", f"{within}\none too many")
        self.assertEqual(len(over), 1)
        self.assertIn(f"{_BODY_MAX + 1} non-blank lines", over[0])

    def test_trailers_do_not_count_toward_the_body(self) -> None:
        body = "\n".join(f"line {n}" for n in range(_BODY_MAX))
        body += "\n\nCo-authored-by: Someone <s@example.com>\nCloses: #12"
        self.assertEqual(_violations("fix: a thing", body), [])

    def test_a_trailer_shaped_line_above_real_prose_still_counts(self) -> None:
        body = "\n".join(f"line {n}" for n in range(_BODY_MAX))
        body += "\nNote: one more\nand trailing prose"
        self.assertIn("non-blank lines", _violations("fix: a thing", body)[0])

    def test_a_body_not_separated_from_the_subject_is_refused(self) -> None:
        found = msg.violations(["fix: a thing", "straight into prose"], _SUBJECT_MAX, _BODY_MAX)
        self.assertEqual(found, ["no blank line between the subject and the body"])

    def test_blank_lines_inside_the_body_are_not_counted(self) -> None:
        self.assertEqual(_violations("fix: a thing", "one\n\ntwo\n\nthree"), [])


class MessageParsingTest(unittest.TestCase):
    def test_comment_lines_and_the_scissors_tail_are_dropped(self) -> None:
        raw = (
            "fix: a thing\n"
            "\n"
            "real prose\n"
            "# Please enter the commit message for your changes.\n"
            "# ------------------------ >8 ------------------------\n"
            "diff --git a/x b/x\n" + "noise\n" * 40
        )
        self.assertEqual(msg.message_lines(raw), ["fix: a thing", "", "real prose"])

    def test_trailing_blank_lines_are_dropped(self) -> None:
        self.assertEqual(msg.message_lines("subject\n\n\n\n"), ["subject"])


class GeneratedMessageTest(unittest.TestCase):
    def test_messages_git_composes_are_left_alone(self) -> None:
        body = "\n".join(f"line {n}" for n in range(40))
        for subject in (
            "Merge branch 'main' into feature",
            'Revert "fix: a thing"',
            "fixup! fix: a thing",
            "squash! fix: a thing",
            "amend! fix: a thing",
        ):
            with self.subTest(subject=subject):
                self.assertEqual(_run_main(f"{subject}\n\n{body}"), (0, ""))

    def test_an_empty_message_is_left_to_git(self) -> None:
        self.assertEqual(_run_main("# all comments\n"), (0, ""))

    def test_an_over_long_message_is_refused_and_says_why(self) -> None:
        code, printed = _run_main("x" * 200)
        self.assertEqual(code, 1)
        self.assertIn("subject is 200 characters", printed)

    def test_an_unreadable_message_file_does_not_block_the_commit(self) -> None:
        absent = Path(tempfile.mkdtemp()) / "no-such-file"

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = msg.main(["check_commit_message.py", str(absent)])

        self.assertEqual(code, 0)
        self.assertIn("not blocking", buffer.getvalue())


class CommentClassifyTest(unittest.TestCase):
    def test_each_banned_class_is_named(self) -> None:
        cases = {
            "# ------------------------------": "section banner",
            "# ===== ": "section banner",
            "# Step 2: wire it up": "section banner",
            "# TODO: come back to this": "TODO/FIXME marker",
            "# FIXME(kfox): broken": "TODO/FIXME marker",
            "# import os": "commented-out code",
            "# self.value = compute(other)": "commented-out code",
            "# for item in items:": "commented-out code",
            "# return None": "commented-out code",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(lint.classify(line), expected)

    def test_the_allowed_classes_are_not_flagged(self) -> None:
        for line in (
            "# type: ignore[arg-type]",
            "# noqa: E501",
            "#!/usr/bin/env python3",
            "# pyright: reportUnknownMemberType=false",
            "#:schema ./schema.json",
            "# See https://github.com/kfox/c64cast/issues/1 for the upstream bug",
            "# The sampler clock ships as 6160000 Hz, not the nominal 6.25 MHz",
            "    # Indented prose about a hardware quirk",
            "value = 3  # a trailing comment is not a standalone one",
            "",
        ):
            with self.subTest(line=line):
                self.assertIsNone(lint.classify(line))

    def test_a_labeled_prose_comment_is_not_read_as_code(self) -> None:
        """`Label: prose` parses as an annotation, which is how comments open."""
        for line in (
            "# Floor: CIA #1 Timer A is a 16-bit down-counter, so the slowest",
            "# Note: the offsets shift uniformly",
            "# init: LDA #initBank / STA $01 / JSR init",
        ):
            with self.subTest(line=line):
                self.assertIsNone(lint.classify(line))

    def test_a_short_word_is_not_read_as_code(self) -> None:
        self.assertIsNone(lint.classify("# ok"))


class StagedDiffTest(unittest.TestCase):
    """The diff walk, against a real repository rather than a crafted string.

    Every `GIT_*` variable is dropped for the duration. `git commit` exports
    `GIT_DIR` and `GIT_INDEX_FILE` to its hooks, and the suite runs inside that
    hook: left in place they outrank `-C`, so these fixtures would stage into
    the checkout's own index and `git diff --cached` would read it back.
    """

    def setUp(self) -> None:
        without_git = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        patcher = mock.patch.dict(os.environ, without_git, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.repo = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def stage(self, text: str, name: str = "m.py") -> None:
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", name)

    def test_the_fixture_runs_with_no_inherited_git_environment(self) -> None:
        self.assertEqual([name for name in os.environ if name.startswith("GIT_")], [])

    def test_the_fixture_stages_into_its_own_repository(self) -> None:
        self.stage("a = 1\n")
        tracked = subprocess.run(
            ["git", "-C", str(self.repo), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertEqual(tracked, ["m.py"])

    def test_an_added_banned_comment_is_found_with_its_line_number(self) -> None:
        self.stage("a = 1\n# TODO: fix this\nb = 2\n")
        self.assertEqual(
            lint.findings(["m.py"]),
            [("m.py", 2, "TODO/FIXME marker", "# TODO: fix this")],
        )

    def test_a_comment_already_committed_is_not_this_commits_business(self) -> None:
        self.stage("# TODO: pre-existing\na = 1\n")
        self.git("commit", "-qm", "seed")

        self.stage("# TODO: pre-existing\na = 1\nb = 2\n")
        self.assertEqual(lint.findings(["m.py"]), [])

    def test_line_numbers_survive_several_hunks(self) -> None:
        self.stage("\n".join(f"x{n} = {n}" for n in range(20)) + "\n")
        self.git("commit", "-qm", "seed")

        lines = [f"x{n} = {n}" for n in range(20)]
        lines.insert(2, "# TODO: early")
        lines.insert(15, "# ----------------")
        self.stage("\n".join(lines) + "\n")

        self.assertEqual(
            [(number, banned) for _, number, banned, _ in lint.findings(["m.py"])],
            [(3, "TODO/FIXME marker"), (16, "section banner")],
        )

    def test_nothing_staged_reports_nothing(self) -> None:
        (self.repo / "m.py").write_text("# TODO: unstaged\n", encoding="utf-8")
        self.assertEqual(lint.findings(["m.py"]), [])

    def test_a_non_python_path_is_ignored(self) -> None:
        self.stage("# TODO: in a text file\n", name="notes.txt")
        self.assertEqual(lint.main(["lint_comments.py", "notes.txt"]), 0)
