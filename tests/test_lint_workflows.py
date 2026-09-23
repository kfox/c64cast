"""Guards for scripts/lint_workflows.py — the workflow reader and `needs:` lint.

Two things are asserted here. That the lint reports each shape it claims to,
against workflows written for the purpose: a lint nobody has watched fail is a
lint that reports nothing, and this one is all that stands between a dangling
`needs:` and a dispatched run. And that this repository's own
`.github/workflows/` passes it, which is what carries the check into
`make check` and CI's test matrix; the commit hook covers the other side, a
workflow edited without touching any Python.

The permission-reading cases live here rather than beside ci.yml's guards
because `effective_permissions` is shared: ci.yml's OIDC coverage upload and
release.yml's Trusted Publishing upload ask it the same question.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")


def _load_lint_workflows():
    """Import scripts/lint_workflows.py by path; `scripts/` is not a package."""
    path = os.path.join(_SCRIPTS, "lint_workflows.py")
    spec = importlib.util.spec_from_file_location("lint_workflows", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lint_workflows"] = module
    spec.loader.exec_module(module)
    return module


wf = _load_lint_workflows()

_HEADER = "name: T\non:\n  push:\n    branches: [main]\n"

_CLEAN = """\
  build:
    runs-on: ubuntu-latest
  publish:
    needs: build
    runs-on: ubuntu-latest
"""


def _parse(jobs_yaml: str):
    """A whole workflow around a `jobs:` body indented two spaces."""
    return wf.parse(f"{_HEADER}jobs:\n{jobs_yaml}")


class NeedsGraphTest(unittest.TestCase):
    def test_a_workflow_whose_needs_all_resolve_has_no_problems(self):
        self.assertEqual(wf.problems("t.yml", _parse(_CLEAN)), [])

    def test_a_needs_naming_no_job_is_reported(self):
        found = wf.problems("t.yml", _parse(_CLEAN.replace("needs: build", "needs: biuld")))
        self.assertEqual(len(found), 1, found)
        self.assertIn("`publish`", found[0])
        self.assertIn("`biuld`", found[0])

    def test_a_typo_inside_a_needs_list_is_reported(self):
        found = wf.problems("t.yml", _parse(_CLEAN.replace("needs: build", "needs: [build, tset]")))
        self.assertEqual(len(found), 1, found)
        self.assertIn("`tset`", found[0])

    def test_a_job_needing_itself_is_reported_as_a_cycle(self):
        found = wf.problems("t.yml", _parse(_CLEAN.replace("needs: build", "needs: publish")))
        self.assertEqual(len(found), 1, found)
        self.assertIn("cycle", found[0])

    def test_a_two_job_cycle_is_reported(self):
        found = wf.problems(
            "t.yml", _parse(_CLEAN.replace("  build:\n", "  build:\n    needs: publish\n"))
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("cycle", found[0])

    def test_a_needs_that_is_neither_a_job_id_nor_a_list_is_reported(self):
        unreadable = _CLEAN.replace("needs: build", "needs:\n      of: build")
        found = wf.problems("t.yml", _parse(unreadable))
        self.assertEqual(len(found), 1, found)
        self.assertIn("cannot be read", found[0])

    def test_a_workflow_declaring_no_jobs_is_not_a_pass(self):
        found = wf.problems("t.yml", wf.parse(_HEADER))
        self.assertEqual(len(found), 1, found)
        self.assertIn("no `jobs:`", found[0])


class NeedsExpressionTest(unittest.TestCase):
    """`needs.<job>` in an expression, which GitHub blanks rather than fails."""

    OUTPUT = "${{ needs.build.outputs.version }}"

    def _reader(self, expression: str, declares: str) -> str:
        return (
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "  publish:\n"
            f"{declares}"
            "    steps:\n"
            f"      - run: echo {expression}\n"
        )

    def test_reading_the_output_of_a_declared_job_is_clean(self):
        jobs = self._reader(self.OUTPUT, declares="    needs: build\n")
        self.assertEqual(wf.problems("t.yml", _parse(jobs)), [])

    def test_reading_the_output_of_an_undeclared_job_is_reported(self):
        jobs = self._reader(self.OUTPUT, declares="    runs-on: ubuntu-latest\n")
        found = wf.problems("t.yml", _parse(jobs))
        self.assertEqual(len(found), 1, found)
        self.assertIn("needs.build", found[0])
        self.assertIn("empty string", found[0])

    def test_a_bare_if_expression_is_read_too(self):
        jobs = (
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "  publish:\n"
            "    if: needs.build.result == 'success'\n"
            "    runs-on: ubuntu-latest\n"
        )
        found = wf.problems("t.yml", _parse(jobs))
        self.assertEqual(len(found), 1, found)
        self.assertIn("needs.build", found[0])

    def test_the_index_spelling_of_a_reference_is_read_too(self):
        # `needs['build']` and `needs.build` are the same reference to GitHub.
        jobs = self._reader(
            "${{ needs['build'].outputs.version }}", declares="    runs-on: ubuntu-latest\n"
        )
        found = wf.problems("t.yml", _parse(jobs))
        self.assertEqual(len(found), 1, found)
        self.assertIn("needs.build", found[0])

    def test_a_needs_key_on_another_object_is_not_a_job_reference(self):
        jobs = self._reader(
            "${{ fromJSON(inputs.config).needs.absent }}", declares="    needs: build\n"
        )
        self.assertEqual(wf.problems("t.yml", _parse(jobs)), [])

    def test_the_whole_needs_context_names_no_single_job(self):
        jobs = (
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "  gate:\n"
            "    needs: build\n"
            "    steps:\n"
            "      - run: jq -e 'all' <<< '${{ toJSON(needs) }}'\n"
        )
        self.assertEqual(wf.problems("t.yml", _parse(jobs)), [])


class PermissionReadingTest(unittest.TestCase):
    """`effective_permissions` + `grants_oidc` against the spellings GitHub takes."""

    GRANTING = "permissions:\n  id-token: write\n"
    SILENT = "permissions:\n  contents: read\n"

    def _grants(self, job_body: str, workflow_permissions: str = "") -> bool:
        workflow = wf.parse(f"name: T\n{workflow_permissions}jobs:\n  job:\n{job_body}")
        return wf.grants_oidc(wf.effective_permissions(workflow, wf.jobs(workflow)["job"]))

    def test_a_jobs_own_block_replaces_a_granting_workflow(self):
        self.assertFalse(
            self._grants("    permissions: read-all\n", self.GRANTING),
            "a job that overrides the grant away cannot mint a token the workflow grants",
        )

    def test_a_job_declaring_no_permissions_runs_under_the_workflow_block(self):
        self.assertTrue(self._grants("    runs-on: ubuntu-latest\n", self.GRANTING))

    def test_a_job_whose_permissions_key_is_empty_grants_nothing(self):
        self.assertFalse(self._grants("    permissions:\n", self.GRANTING))

    def test_neither_default_carries_id_token(self):
        self.assertFalse(self._grants("    runs-on: ubuntu-latest\n"))

    def test_a_commented_out_grant_is_not_a_grant(self):
        self.assertFalse(
            self._grants("    permissions:\n      # id-token: write\n", self.GRANTING),
            "a grant someone commented out still reads as granted",
        )

    def test_a_comment_on_the_key_does_not_hide_the_grant(self):
        job = "    permissions: # least privilege\n      id-token: write\n"
        self.assertTrue(self._grants(job, self.SILENT))

    def test_a_quoted_grant_counts(self):
        for value in ("write", "'write'", '"write"'):
            with self.subTest(value=value):
                self.assertTrue(self._grants(f"    permissions:\n      id-token: {value}\n"))

    def test_write_all_counts_however_it_is_spelled(self):
        for value in ("write-all", "write-all # everything", '"write-all"'):
            with self.subTest(value=value):
                self.assertTrue(self._grants(f"    permissions: {value}\n", self.SILENT))

    def test_read_all_is_not_a_grant(self):
        self.assertFalse(self._grants("    permissions: read-all\n", self.SILENT))

    def test_id_token_read_is_not_a_grant(self):
        self.assertFalse(self._grants("    permissions:\n      id-token: read\n"))


class ThisRepositoryTest(unittest.TestCase):
    def test_every_workflow_here_passes_the_lint(self):
        paths = wf.workflow_paths()
        self.assertTrue(paths, "no workflow files found, so nothing was checked")
        found = [problem for path in paths for problem in wf.file_problems(path)]
        self.assertEqual(found, [], "\n".join(found))

    def test_main_reads_this_repositorys_workflows_when_given_no_paths(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = wf.main(["lint_workflows.py"])
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(err.getvalue(), "")


class FileReadingTest(unittest.TestCase):
    def _write(self, name: str, text: str) -> str:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_file_that_is_not_yaml_is_reported_rather_than_raised(self):
        path = self._write("broken.yml", "jobs:\n  build:\n   - [unbalanced\n")
        found = wf.file_problems(path)
        self.assertEqual(len(found), 1, found)
        self.assertIn("does not parse as YAML", found[0])

    def test_a_path_that_cannot_be_opened_is_reported_rather_than_raised(self):
        # The hook reaches these: a directory named `x.yml` beside the
        # workflows, a broken symlink, a path typed by hand at the prompt.
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / "a-directory.yml").mkdir()
        for name in ("a-directory.yml", "missing.yml"):
            with self.subTest(name=name):
                found = wf.file_problems(str(directory / name))
                self.assertEqual(len(found), 1, found)
                self.assertIn("could not be read", found[0])

    def test_a_file_that_is_not_utf8_is_reported_rather_than_raised(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "latin1.yml"
        path.write_bytes(b"name: caf\xe9\njobs:\n  build:\n    runs-on: ubuntu-latest\n")
        found = wf.file_problems(str(path))
        self.assertEqual(len(found), 1, found)
        self.assertIn("could not be read", found[0])

    def test_main_names_the_problem_and_exits_nonzero(self):
        dangling = _CLEAN.replace("needs: build", "needs: tset")
        path = self._write("t.yml", f"{_HEADER}jobs:\n{dangling}")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = wf.main(["lint_workflows.py", path])
        self.assertEqual(status, 1)
        self.assertIn("`tset`", err.getvalue())

    def test_a_directory_with_no_workflows_is_not_a_pass(self):
        empty = self.enterContext(tempfile.TemporaryDirectory())
        err = io.StringIO()
        with mock.patch.object(wf, "WORKFLOW_DIR", empty), contextlib.redirect_stderr(err):
            status = wf.main(["lint_workflows.py"])
        self.assertEqual(status, 1)
        self.assertIn("nothing was checked", err.getvalue())


if __name__ == "__main__":
    unittest.main()
