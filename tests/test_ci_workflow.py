"""Guards for .github/workflows/ci.yml.

Branch protection requires the gate's context in place of one per matrix leg,
so its `needs:` list is what decides which jobs can block a merge, and
`if: always()` is what makes it report — GitHub counts a skipped job as a pass.

The `use_oidc` input and the job's `id-token: write` permission have to travel
together: without the permission the action's token step throws before the
upload runs, and the error it raises names neither of them.

Which block actually grants that is `scripts/lint_workflows.py`'s answer, the
same one release.yml's guards ask for — a job's `permissions:` replaces the
workflow-level block rather than merging with it, and a second reader of that
rule is a second reader to keep correct.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")
_WORKFLOW = os.path.join(_REPO, ".github", "workflows", "ci.yml")


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

_CI = wf.load(_WORKFLOW)
_JOBS = wf.jobs(_CI)

_GATE = "ci"


def _with_blocks(job: object) -> list[object]:
    """Every `with:` a job passes inputs through — its steps', and its own.

    A job that calls a reusable workflow has no `steps:`; its inputs sit in a
    `with:` beside the job's `uses:`, and `permissions:` there is what the
    called workflow runs under. Reading only the steps would skip such a job,
    which reads back from the loop below exactly like a job that grants
    nothing because it needs nothing.
    """
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    holders = [job, *(steps if isinstance(steps, list) else [])]
    return [holder.get("with") for holder in holders if isinstance(holder, dict)]


def _asks_for_oidc(job: object) -> bool:
    """Whether a job uploads with OIDC — a value this cannot read counts as yes."""
    for inputs in _with_blocks(job):
        if not isinstance(inputs, dict) or "use_oidc" not in inputs:
            continue
        if str(inputs["use_oidc"]).strip().lower() != "false":
            return True
    return False


def _oidc_uploaders() -> list[str]:
    return [job_id for job_id, job in _JOBS.items() if _asks_for_oidc(job)]


class AggregateGateTest(unittest.TestCase):
    def test_the_gate_needs_every_other_job(self):
        needs = wf.needs_of(_JOBS[_GATE])
        self.assertIsNotNone(needs, f"the `{_GATE}` job's `needs:` cannot be read")
        assert needs is not None
        self.assertEqual(
            set(_JOBS) - {_GATE},
            set(needs),
            "a job outside the gate's `needs:` can go red without blocking a merge",
        )

    def test_the_gate_reports_when_a_dependency_fails(self):
        self.assertIn(
            "always()",
            str(_JOBS[_GATE].get("if", "")),
            "without `always()` the gate skips, and a skipped check satisfies branch protection",
        )


class OidcUploadTest(unittest.TestCase):
    def test_a_job_uploading_with_oidc_can_mint_its_token(self):
        for job_id in _oidc_uploaders():
            with self.subTest(job=job_id):
                self.assertTrue(
                    wf.grants_oidc(wf.effective_permissions(_CI, _JOBS[job_id])),
                    f"the `{job_id}` job uploads with OIDC but cannot mint a token, "
                    "so the action's token step throws before the upload runs",
                )

    def test_a_job_here_really_does_upload_with_oidc(self):
        """A grant check that examines no job reads back exactly like a pass."""
        self.assertTrue(
            _oidc_uploaders(),
            "no job asks for OIDC any more, so nothing was checked for a grant",
        )


class OidcDetectionTest(unittest.TestCase):
    """`_asks_for_oidc` against the places a workflow can pass the input."""

    def test_a_step_asking_for_oidc_is_found(self):
        self.assertTrue(_asks_for_oidc({"steps": [{"with": {"use_oidc": True}}]}))

    def test_a_reusable_workflow_call_asking_for_oidc_is_found(self):
        self.assertTrue(
            _asks_for_oidc({"uses": "./.github/workflows/up.yml", "with": {"use_oidc": True}})
        )

    def test_turning_the_input_off_is_not_asking(self):
        self.assertFalse(_asks_for_oidc({"steps": [{"with": {"use_oidc": False}}]}))

    def test_a_value_that_cannot_be_read_counts_as_asking(self):
        self.assertTrue(_asks_for_oidc({"steps": [{"with": {"use_oidc": "${{ inputs.oidc }}"}}]}))

    def test_a_job_passing_no_inputs_is_not_asking(self):
        self.assertFalse(_asks_for_oidc({"steps": [{"run": "make coverage"}]}))
