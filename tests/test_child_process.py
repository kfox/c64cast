"""`run_bounded` — the bound a child process started by a test module runs under.

Two halves. The helper itself: what it returns, what it does when the bound
expires, and that the bound it ships sits where `tests/_child_process.py` says
it does relative to the per-test cap. And the sweep that closes the class,
because a bound nobody is obliged to use is a bound the next test omits — all
twelve call sites in this suite omitted it, and the one that eventually hung
was found by CI on Windows rather than by anything here.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

import _child_process
import _timeout_sandbox
from _child_process import BOUND_S, run_bounded

#: Short enough that driving a real expiry costs a fraction of a second.
_TEST_BOUND_S = 0.3

#: The bound for an expiry whose assertion is about what the child wrote.
#: Those tests need the child to reach its `print` before it is killed, and
#: `_TEST_BOUND_S` does not clear interpreter startup with a margin: starting
#: `sys.executable` with `PYTHONPATH=tests` measures ~0.1 s on a warm
#: workstation and several times that on a loaded Windows runner, which is the
#: machine this whole module exists because of.
_PRINTED_BOUND_S = 2.0


def _hangs_after(statement: str = "pass") -> list[str]:
    """A child that runs `statement`, flushes, and then never exits.

    300 s rather than a bare block: if the bound under test ever stopped
    working, this fails the per-test cap in a minute instead of holding the
    worker until CI gives up on the whole job.
    """
    return [
        sys.executable,
        "-c",
        f"import sys, time; {statement}; sys.stdout.flush(); sys.stderr.flush(); time.sleep(300)",
    ]


class RunBoundedTest(unittest.TestCase):
    def test_a_child_that_exits_returns_its_completed_process(self):
        proc = run_bounded(
            [sys.executable, "-c", "print('hi')"], capture_output=True, text=True, check=True
        )
        self.assertEqual(proc.stdout.strip(), "hi")

    def test_the_shipped_bound_applies_when_the_caller_names_none(self):
        with patch.object(subprocess, "run") as run:
            run_bounded(["true"])
        self.assertEqual(run.call_args.kwargs["timeout"], BOUND_S)

    def test_a_named_bound_wins_over_the_shipped_one(self):
        with patch.object(subprocess, "run") as run:
            run_bounded(["true"], timeout=1.5)
        self.assertEqual(run.call_args.kwargs["timeout"], 1.5)

    def test_every_other_keyword_reaches_subprocess_run(self):
        with patch.object(subprocess, "run") as run:
            run_bounded(["true"], capture_output=True, text=True, check=True, cwd="/")
        self.assertEqual(
            {k: v for k, v in run.call_args.kwargs.items() if k != "timeout"},
            {"capture_output": True, "text": True, "check": True, "cwd": "/"},
        )

    def test_a_child_that_outlives_the_bound_fails_naming_the_command(self):
        with self.assertRaises(AssertionError) as caught:
            run_bounded(_hangs_after(), timeout=_TEST_BOUND_S, capture_output=True)
        message = str(caught.exception)
        self.assertIn(f"did not exit within {_TEST_BOUND_S:g}s", message)
        self.assertIn("time.sleep(300)", message)

    def test_what_the_killed_child_wrote_is_in_the_failure(self):
        # The stream it died holding is usually why: a child that hung after
        # printing hung on the last thing it said.
        with self.assertRaises(AssertionError) as caught:
            run_bounded(
                _hangs_after("print('reached step 3')"),
                timeout=_PRINTED_BOUND_S,
                capture_output=True,
                text=True,
            )
        self.assertIn("reached step 3", str(caught.exception))

    def test_a_child_that_wrote_nothing_adds_no_empty_section(self):
        with self.assertRaises(AssertionError) as caught:
            run_bounded(_hangs_after(), timeout=_TEST_BOUND_S, capture_output=True)
        self.assertNotIn("stdout:", str(caught.exception))

    def test_a_long_stream_is_tailed_rather_than_dumped(self):
        with self.assertRaises(AssertionError) as caught:
            run_bounded(
                _hangs_after("sys.stdout.write('x' * 5000)"),
                timeout=_PRINTED_BOUND_S,
                capture_output=True,
                text=True,
            )
        self.assertIn("stdout: ...", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 1200)

    def test_bytes_from_an_uncaptured_text_child_still_render(self):
        with self.assertRaises(AssertionError) as caught:
            run_bounded(
                _hangs_after("sys.stderr.buffer.write(b'\\xff bad')"),
                timeout=_PRINTED_BOUND_S,
                capture_output=True,
            )
        self.assertIn("bad", str(caught.exception))


class BoundSitsBelowThePerTestCapTest(unittest.TestCase):
    """The relation the bound exists for, pinned so raising it goes red.

    A bound at or above `_timeout_sandbox._CAP_S` would never fire: the cap
    would interrupt the test first and report "no progress", which is the
    message this whole change exists to stop getting. The shipped default
    rather than `configured_cap()`, because the relationship is a property of
    the two constants, not of whatever `C64CAST_TEST_TIMEOUT_S` a developer
    stepping through a test happens to have exported.
    """

    def test_two_expirations_still_fit_under_the_per_test_cap(self):
        self.assertLess(BOUND_S * 2, _timeout_sandbox._CAP_S)

    def test_the_bound_clears_the_slowest_child_this_suite_runs(self):
        # Measured at 0.73 s over the 155 children a full run starts. An order
        # of magnitude is the floor: below it, a loaded CI runner fails a
        # healthy child.
        self.assertGreater(BOUND_S, 10.0)


class EveryChildProcessIsBoundTest(unittest.TestCase):
    """No module under `tests/` reaches `subprocess` without a bound.

    The class, not the instance. `test_page_assets` is where the hang surfaced,
    but every one of the twelve call sites in this suite was written the same
    way, and so was the thirteenth before this sweep existed to refuse it.
    Nothing here is exempt, including `_child_process.py`: it passes because it
    names `timeout=`, which is cheaper to keep true than an allowlist is to
    keep current.
    """

    #: Names on the `subprocess` module that start no child, so a call to one
    #: needs no bound. Everything else does — including `Popen`, which takes no
    #: `timeout` at all and so can never satisfy this sweep. That is the
    #: intended answer: a `Popen`'s bound belongs on its `communicate`, nothing
    #: in this suite needs one, and the reader who does should extend
    #: `_child_process.py` rather than hand-roll it.
    #:
    #: An allowlist rather than a list of starters, so a name this sweep has
    #: never heard of is refused rather than waved through — the direction a
    #: guard has to fail in to be worth having.
    NON_STARTING = frozenset(
        {
            "CalledProcessError",
            "CompletedProcess",
            "STARTUPINFO",
            "SubprocessError",
            "TimeoutExpired",
            "list2cmdline",
        }
    )

    @staticmethod
    def _imports(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
        """`(module aliases, {local name: subprocess name})` for `tree`.

        `ast.walk` rather than the module body: a test that imports
        `subprocess` inside the one method that needs it reaches the same
        module by the same name, and `test_audio_source_sid` was written that
        way until this change.
        """
        modules: set[str] = set()
        names: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.asname or a.name for a in node.names if a.name == "subprocess"}
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                names.update({a.asname or a.name: a.name for a in node.names})
        return modules, names

    @classmethod
    def _starter(cls, call: ast.Call, modules: set[str], names: dict[str, str]) -> str | None:
        """What `call` starts a child with, or None if it starts none.

        Both spellings that reach the module: `subprocess.run(...)` under any
        alias, and a `run` pulled out with `from subprocess import`. The
        receiver may itself be an attribute — `upgrade.subprocess.run(...)`
        reaches exactly the same function as the bare name does.
        """
        func = call.func
        if isinstance(func, ast.Attribute):
            recv = func.value
            owner = recv.id if isinstance(recv, ast.Name) else getattr(recv, "attr", None)
            if owner in modules and func.attr not in cls.NON_STARTING:
                return f"{owner}.{func.attr}"
            return None
        if isinstance(func, ast.Name) and names.get(func.id) not in (None, *cls.NON_STARTING):
            return f"{func.id}() [from subprocess import {names[func.id]}]"
        return None

    @staticmethod
    def _bounds(kw: ast.keyword) -> bool:
        """Whether `kw` is a `timeout=` that actually bounds the call.

        `timeout=None` is what `subprocess` itself spells "wait forever", so
        it is refused exactly like an omitted keyword — otherwise the one
        wording that means no bound is the one wording that satisfies a sweep
        demanding a bound.
        """
        if kw.arg != "timeout":
            return False
        return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)

    @classmethod
    def offenders_in(cls, source: str, label: str) -> list[str]:
        """Every unbounded child-starting call in `source`.

        A literal `timeout=` keyword that is not `None`, and nothing else.
        `**kwargs` is not accepted as one: whether it carries a bound is a
        fact about the caller, and a sweep that took the possibility for an
        answer would pass every call site in the suite by writing `**{}`.
        """
        tree = ast.parse(source)
        modules, names = cls._imports(tree)
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            starter = cls._starter(node, modules, names)
            if starter and not any(cls._bounds(kw) for kw in node.keywords):
                found.append(f"{label}:{node.lineno} -> {starter}")
        return found

    def test_no_test_module_starts_a_child_without_a_bound(self):
        offenders: list[str] = []
        root = pathlib.Path(__file__).resolve().parent
        for path in sorted(root.rglob("*.py")):
            offenders += self.offenders_in(
                path.read_text(encoding="utf-8"), str(path.relative_to(root.parent))
            )
        self.assertEqual(
            offenders,
            [],
            "these calls start a child process with nothing bounding it, so one that "
            "never exits blocks until the 60s per-test cap reports 'no progress' "
            "instead of naming it; call run_bounded() from tests/_child_process.py, "
            "or pass a timeout= of your own that is not None:\n  " + "\n  ".join(offenders),
        )


class SweepDepthTest(unittest.TestCase):
    """What the sweep can actually see, pinned against a narrowing.

    A sweep reports the same empty list whether the tree is clean or it is not
    looking, so the shapes it has to catch are written out here rather than
    trusted. Each probe below is a spelling that reaches `subprocess` — and
    each negative is one that does not, because a guard that cries about a
    local function named `run` gets switched off. That last one is not
    hypothetical: the first scan written for this change flagged three of them
    in `test_backend.py`, `test_perf_console.py` and `test_process_exit.py`.
    """

    def offenders(self, body: str) -> list[str]:
        return EveryChildProcessIsBoundTest.offenders_in(textwrap.dedent(body), "probe.py")

    def test_a_plain_unbounded_run_is_caught(self):
        self.assertTrue(
            self.offenders("""
                import subprocess
                subprocess.run(["git", "status"], capture_output=True)
                """)
        )

    def test_a_bounded_run_is_not(self):
        self.assertEqual(
            self.offenders("""
                import subprocess
                subprocess.run(["git", "status"], timeout=5)
                """),
            [],
        )

    def test_a_timeout_of_none_is_not_a_bound(self):
        self.assertTrue(
            self.offenders("""
                import subprocess
                subprocess.run(["git", "status"], timeout=None)
                """)
        )

    def test_an_aliased_module_is_caught(self):
        self.assertTrue(
            self.offenders("""
                import subprocess as sp
                sp.check_output(["git", "status"])
                """)
        )

    def test_a_name_pulled_out_of_subprocess_is_caught(self):
        self.assertTrue(
            self.offenders("""
                from subprocess import run
                run(["git", "status"])
                """)
        )

    def test_a_renamed_name_pulled_out_of_subprocess_is_caught(self):
        self.assertTrue(
            self.offenders("""
                from subprocess import check_output as co
                co(["git", "status"])
                """)
        )

    def test_an_import_inside_the_method_that_uses_it_is_caught(self):
        self.assertTrue(
            self.offenders("""
                def test_thing(self):
                    import subprocess
                    subprocess.check_output(["git", "status"])
                """)
        )

    def test_the_module_reached_through_another_module_is_caught(self):
        self.assertTrue(
            self.offenders("""
                import subprocess
                upgrade.subprocess.run(["git", "status"])
                """)
        )

    def test_popen_can_never_satisfy_the_sweep(self):
        # It takes no `timeout`, so there is no spelling of it that passes.
        self.assertTrue(
            self.offenders("""
                import subprocess
                subprocess.Popen(["git", "status"])
                """)
        )

    def test_kwargs_unpacking_is_not_a_bound(self):
        self.assertTrue(
            self.offenders("""
                import subprocess
                subprocess.run(["git", "status"], **kw)
                """)
        )

    def test_constructing_a_subprocess_exception_is_not_starting_a_child(self):
        self.assertEqual(
            self.offenders("""
                import subprocess
                raise subprocess.TimeoutExpired("git", 10)
                """),
            [],
        )

    def test_the_same_exception_pulled_out_by_name_is_not_either(self):
        self.assertEqual(
            self.offenders("""
                from subprocess import TimeoutExpired
                raise TimeoutExpired("git", 10)
                """),
            [],
        )

    def test_a_local_function_named_run_is_not_subprocess(self):
        self.assertEqual(
            self.offenders("""
                def run():
                    return 1
                run()
                """),
            [],
        )

    def test_a_call_on_an_unrelated_receiver_named_like_a_starter_is_not(self):
        self.assertEqual(
            self.offenders("""
                import subprocess
                pool.run(["git", "status"])
                """),
            [],
        )

    def test_a_name_never_imported_from_subprocess_is_not(self):
        self.assertEqual(
            self.offenders("""
                from concurrent.futures import ThreadPoolExecutor as run
                run(["git", "status"])
                """),
            [],
        )


class SweepReachTest(unittest.TestCase):
    """The sweep reads the directory it claims to, including itself."""

    def test_every_module_under_tests_is_read(self) -> None:
        root = pathlib.Path(__file__).resolve().parent
        swept = {p.name for p in root.rglob("*.py")}
        self.assertIn("_child_process.py", swept)
        self.assertIn(pathlib.Path(__file__).name, swept)
        self.assertIn("test_page_assets.py", swept)

    def test_the_helpers_own_call_passes_because_it_names_the_bound(self) -> None:
        source = pathlib.Path(_child_process.__file__).read_text(encoding="utf-8")
        self.assertIn("subprocess.run(", source)
        self.assertEqual(EveryChildProcessIsBoundTest.offenders_in(source, "_child_process.py"), [])


if __name__ == "__main__":
    unittest.main()
