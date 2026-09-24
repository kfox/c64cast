"""Tests for the environment guard and the Makefile wiring that runs it.

The guard's failure mode is a silent pass: it answers "which checkout does this
environment's `c64cast` come from", and any bug that makes it answer with the
*current* checkout makes it agree with itself forever — so the isolation is
pinned against a real interpreter rather than assumed, and the parse that
decides which targets need it fails closed on any line it cannot read whole.

scripts/ is not a package, so the module is loaded by path (as
tests/test_mutation_ready.py does).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _child_process import run_bounded
from _fakes import tmp_cwd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAKEFILE = _REPO_ROOT / "Makefile"

_TARGET = re.compile(r"^([A-Za-z][\w./-]*)[ \t]*:(?!=)[ \t]*(.*)$")
_ECHO = re.compile(r"^\t@?echo\b")
_DEFINE = re.compile(r"^define[ \t]+([\w.-]+)")
_CALL = re.compile(r"\$\(call[ \t]+([\w.-]+)")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][\w.]*[ \t]*[:+?!]?=")
_INCLUDE = re.compile(r"^[-s]?include[ \t]")
_REACHES_UV = re.compile(r"\buv\b|\$\(PY\)")
_GUARDS = frozenset({"$(SYNC)", "$(GUARD)", "venv-check"})


def _makefile_source() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_script("check_venv_target")


def _run_main(checkout: Path, imported: Path | None) -> tuple[int, str]:
    """Drive `main()` with the two lookups stubbed, returning (code, stderr)."""
    buffer = io.StringIO()
    with (
        mock.patch.object(check, "_checkout_root", return_value=checkout),
        mock.patch.object(check, "_environment_python", return_value=Path(sys.executable)),
        mock.patch.object(check, "_query_package_root", return_value=(True, imported)),
        contextlib.redirect_stderr(buffer),
    ):
        code = check.main()
    return code, buffer.getvalue()


class EnvironmentPythonTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_a_posix_interpreter_is_found(self):
        env = self.root / ".venv" / "bin"
        env.mkdir(parents=True)
        (env / "python").write_text("", encoding="utf-8")
        self.assertEqual(check._environment_python(self.root, None), env / "python")

    def test_a_windows_interpreter_is_found(self):
        env = self.root / ".venv" / "Scripts"
        env.mkdir(parents=True)
        (env / "python.exe").write_text("", encoding="utf-8")
        self.assertEqual(check._environment_python(self.root, None), env / "python.exe")

    def test_an_absent_environment_is_not_an_interpreter(self):
        self.assertIsNone(check._environment_python(self.root, None))

    def test_a_relative_configured_environment_resolves_against_the_checkout(self):
        env = self.root / "relenv" / "bin"
        env.mkdir(parents=True)
        (env / "python").write_text("", encoding="utf-8")
        self.assertEqual(check._environment_python(self.root, "relenv"), env / "python")

    def test_a_configured_environment_outranks_the_default(self):
        configured = self.root / "elsewhere" / "bin"
        configured.mkdir(parents=True)
        (configured / "python").write_text("", encoding="utf-8")
        default = self.root / ".venv" / "bin"
        default.mkdir(parents=True)
        (default / "python").write_text("", encoding="utf-8")
        found = check._environment_python(self.root, str(configured.parent))
        self.assertEqual(found, configured / "python")


class VerdictTest(unittest.TestCase):
    def test_an_environment_from_this_checkout_passes_quietly(self):
        code, err = _run_main(Path("/repo"), Path("/repo"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_an_environment_from_another_checkout_fails(self):
        code, err = _run_main(Path("/repo"), Path("/elsewhere"))
        self.assertEqual(code, 1)

    def test_the_failure_names_both_checkouts_and_the_fix(self):
        checkout, imported = Path("/repo"), Path("/elsewhere")

        _, err = _run_main(checkout, imported)

        self.assertIn(str(checkout), err)
        self.assertIn(str(imported), err)
        self.assertIn("UV_PROJECT_ENVIRONMENT", err)

    def test_an_environment_with_no_package_installed_cannot_tell_and_passes(self):
        code, err = _run_main(Path("/repo"), None)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


def _run_with_environment(checkout: Path, configured: str | None) -> tuple[int, str]:
    """Drive `main()` against a real directory layout, returning (code, stderr)."""
    environ = {k: v for k, v in os.environ.items() if k != "UV_PROJECT_ENVIRONMENT"}
    if configured is not None:
        environ["UV_PROJECT_ENVIRONMENT"] = configured

    buffer = io.StringIO()
    with (
        mock.patch.object(check, "_checkout_root", return_value=checkout),
        mock.patch.dict(os.environ, environ, clear=True),
        contextlib.redirect_stderr(buffer),
    ):
        code = check.main()
    return code, buffer.getvalue()


class UnrunnableInterpreterTest(unittest.TestCase):
    """An interpreter that cannot be asked what it imports — dangling, or not executable.

    A venv outlives the base Python it was built on, leaving `bin/python`
    dangling; one restored from an archive can lose its executable bit. uv will
    happily rebuild either from this source, so passing it over is the silent
    pass — but the same break in this checkout's own `.venv` must not block
    `make sync`, which is the repair.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _dangling_environment(self, env_dir: Path) -> Path:
        interpreter = env_dir / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to("/nonexistent/python")
        return interpreter

    def _non_executable_environment(self, env_dir: Path) -> Path:
        interpreter = env_dir / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        interpreter.chmod(0o644)
        return interpreter

    def test_a_dangling_interpreter_is_found_rather_than_read_as_absent(self):
        interpreter = self._dangling_environment(self.root / ".venv")
        self.assertEqual(check._environment_python(self.root, None), interpreter)

    def test_an_outside_environment_that_cannot_run_is_reported(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        foreign = self.root / "foreign"
        self._dangling_environment(foreign)

        code, err = _run_with_environment(checkout, str(foreign))

        self.assertEqual(code, 1)
        self.assertIn("does not run", err)
        self.assertIn("UV_PROJECT_ENVIRONMENT", err)

    def test_an_outside_interpreter_that_will_not_execute_is_reported(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        foreign = self.root / "foreign"
        self._non_executable_environment(foreign)

        code, err = _run_with_environment(checkout, str(foreign))

        self.assertEqual(code, 1)
        self.assertIn("does not run", err)

    def test_this_checkouts_own_broken_environment_leaves_the_repair_open(self):
        self._dangling_environment(self.root / ".venv")

        code, err = _run_with_environment(self.root, None)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_repair_stays_open_through_a_symlinked_path_to_the_checkout(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        self._dangling_environment(checkout / ".venv")
        reached_by = self.root / "link"
        reached_by.symlink_to(checkout)

        code, err = _run_with_environment(checkout, str(reached_by / ".venv"))

        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_an_environment_symlinked_out_of_the_checkout_is_not_exempt(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        shared = self.root / "shared"
        self._dangling_environment(shared)
        (checkout / ".venv").symlink_to(shared)

        code, err = _run_with_environment(checkout, None)

        self.assertEqual(code, 1)
        self.assertIn("does not run", err)


class InterpreterIsolationTest(unittest.TestCase):
    """Only the environment may answer the lookup — not the cwd, not `PYTHONPATH`."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.env = self.root / "env"
        run_bounded(
            [sys.executable, "-m", "venv", "--without-pip", str(self.env)],
            check=True,
            capture_output=True,
        )
        site = next(self.env.glob("lib/python*/site-packages"), None)
        if site is None:  # Windows layout
            site = self.env / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        (site / "c64cast").mkdir()
        (site / "c64cast" / "__init__.py").write_text("", encoding="utf-8")
        self.python = next(
            p
            for p in (self.env / "bin" / "python", self.env / "Scripts" / "python.exe")
            if p.exists()
        )

    def test_the_installed_package_is_what_is_reported(self):
        ran, found = check._query_package_root(self.python)
        self.assertTrue(ran)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertNotEqual(found, _REPO_ROOT)

    def test_no_working_directory_can_answer_the_lookup(self):
        with tmp_cwd() as decoy:
            package = Path(decoy) / "c64cast"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            _, found = check._query_package_root(self.python)

        self.assertNotEqual(found, Path(decoy).resolve())

    def test_pythonpath_cannot_satisfy_the_lookup_either(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(_REPO_ROOT)}):
            _, found = check._query_package_root(self.python)

        self.assertNotEqual(found, _REPO_ROOT)


def _makefile_targets() -> dict[str, tuple[list[str], list[str]]]:
    """Map each Makefile target to its (prerequisites, recipe lines)."""
    targets: dict[str, tuple[list[str], list[str]]] = {}
    current = ""
    in_define = False

    for line in _makefile_source().splitlines():
        if _DEFINE.match(line):
            in_define = True
        elif line.startswith("endef"):
            in_define = False
        elif in_define:
            continue
        elif line.startswith("\t"):
            if current and not _ECHO.match(line):
                targets[current][1].append(line)
        elif match := _TARGET.match(line):
            current = match.group(1)
            targets.setdefault(current, (match.group(2).split(), []))

    return targets


def _reads_whole(line: str) -> bool:
    """Whether `_TARGET` reads a rule line whole — recipe on its own lines, not after `;`."""
    match = _TARGET.match(line)
    return match is not None and ";" not in match.group(2)


def _unreadable_makefile_lines(source: str) -> list[str]:
    """Lines the target parse drops silently: an unreadable rule, or an `include`."""
    unreadable: list[str] = []
    in_define = False

    for line in source.splitlines():
        if _DEFINE.match(line):
            in_define = True
        elif line.startswith("endef"):
            in_define = False
        elif (
            in_define
            or not line.strip()
            or line.startswith(("\t", " ", "#", "."))
            or _ASSIGNMENT.match(line)
        ):
            continue
        elif _INCLUDE.match(line) or (":" in line and not _reads_whole(line)):
            unreadable.append(line)

    return unreadable


def _uv_reaching_defines() -> frozenset[str]:
    """Names of the `define` blocks whose body reaches uv."""
    reaching: set[str] = set()
    defining = ""

    for line in _makefile_source().splitlines():
        if match := _DEFINE.match(line):
            defining = match.group(1)
        elif line.startswith("endef"):
            defining = ""
        elif defining and _REACHES_UV.search(line):
            reaching.add(defining)

    return frozenset(reaching)


def _reaches_uv(line: str, defines: frozenset[str]) -> bool:
    """Whether a recipe line invokes uv, itself or through a `$(call ...)`."""
    if _REACHES_UV.search(line):
        return True

    return any(name in defines for name in _CALL.findall(line))


def _is_guarded(name: str, targets: dict[str, tuple[list[str], list[str]]]) -> bool:
    prerequisites = targets.get(name, ([], []))[0]
    if _GUARDS.intersection(prerequisites):
        return True

    return any(p in targets and _is_guarded(p, targets) for p in prerequisites)


class MakefileCoverageTest(unittest.TestCase):
    """`uv run` syncs before it executes, so reaching uv at all is the trigger."""

    def setUp(self):
        self.targets = _makefile_targets()

    def test_the_parse_finds_the_known_targets(self):
        self.assertIn("sync", self.targets)
        self.assertIn("site-check", self.targets)
        self.assertNotIn("render-book", self.targets)

    def test_no_rule_line_escapes_the_parse(self):
        self.assertEqual(_unreadable_makefile_lines(_makefile_source()), [])

    def test_a_rule_the_parse_cannot_read_whole_is_reported(self):
        for line in (
            "site site-check: $(GUARD)",
            "docs/%.pdf: docs/%.md",
            "artifacts g2 &: dep",
            "inline: ; uv run scripts/build_site.py",
            "include mk/books.mk",
            "-include mk/books.mk",
        ):
            with self.subTest(line):
                self.assertEqual(_unreadable_makefile_lines(line), [line])

    def test_the_rules_the_parse_does_read_are_left_alone(self):
        self.assertEqual(_unreadable_makefile_lines("site-check: $(GUARD)"), [])

    def test_a_define_that_hides_uv_makes_its_call_sites_reach_uv(self):
        defines = _uv_reaching_defines()
        self.assertIn("render-book", defines)
        self.assertTrue(_reaches_uv("\t$(call render-book,$(GUIDE_DIR),book)", defines))

    def test_every_target_that_reaches_uv_runs_the_guard_first(self):
        defines = _uv_reaching_defines()
        unguarded = [
            name
            for name, (_, recipe) in self.targets.items()
            if any(_reaches_uv(line, defines) for line in recipe)
            and not _is_guarded(name, self.targets)
        ]
        self.assertEqual(unguarded, [])


if __name__ == "__main__":
    unittest.main()
