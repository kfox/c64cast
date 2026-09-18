"""Guards for the aggregate gate in .github/workflows/ci.yml.

Branch protection requires the gate's context in place of one per matrix leg,
so its `needs:` list is what decides which jobs can block a merge, and
`if: always()` is what makes it report — GitHub counts a skipped job as a pass.
"""

from __future__ import annotations

import os
import re
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = os.path.join(_REPO, ".github", "workflows", "ci.yml")

with open(_WORKFLOW, encoding="utf-8") as f:
    _CI = f.read()

_JOB_ID = re.compile(r"^  ([\w-]+):$", re.M)
_INLINE_NEEDS = re.compile(r"^    needs: \[([^\]]+)\]$", re.M)

_GATE = "ci"


def _job_ids() -> set[str]:
    """Every job id in the workflow, by its two-space indent under `jobs:`."""
    return set(_JOB_ID.findall(_CI.partition("\njobs:\n")[2]))


def _job_block(job_id: str) -> str:
    """One job's own lines — those indented deeper than its key."""
    body = _CI.partition(f"\n  {job_id}:\n")[2]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("   "):
            break
        lines.append(line)
    return "\n".join(lines)


class AggregateGateTest(unittest.TestCase):
    def test_the_gate_needs_every_other_job(self):
        needs = _INLINE_NEEDS.search(_job_block(_GATE))
        assert needs is not None, f"the `{_GATE}` job's `needs:` is no longer an inline list"
        self.assertEqual(
            _job_ids() - {_GATE},
            {name.strip() for name in needs.group(1).split(",")},
            "a job outside the gate's `needs:` can go red without blocking a merge",
        )

    def test_the_gate_reports_when_a_dependency_fails(self):
        self.assertIn(
            "always()",
            _job_block(_GATE),
            "without `always()` the gate skips, and a skipped check satisfies branch protection",
        )
