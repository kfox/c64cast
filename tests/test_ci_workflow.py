"""Guards for the aggregate gate in .github/workflows/ci.yml.

Branch protection requires the gate's context in place of one per matrix leg,
so its `needs:` list is what decides which jobs can block a merge, and
`if: always()` is what makes it report — GitHub counts a skipped job as a pass.

A merge queue gates on that same context, evaluated against the merge group it
builds, which is why the workflow triggers on `merge_group` too: a required
check that never reports there does not fail the entry, it holds the queue
until the status-check timeout evicts it.
"""

from __future__ import annotations

import os
import re
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = os.path.join(_REPO, ".github", "workflows", "ci.yml")

with open(_WORKFLOW, encoding="utf-8") as f:
    _CI = f.read()

_BLOCK_KEY = re.compile(r"^  ([\w-]+):$", re.M)
_INLINE_NEEDS = re.compile(r"^    needs: \[([^\]]+)\]$", re.M)

_GATE = "ci"


def _block(key: str) -> str:
    """The lines under `key:`, up to the first one not indented past it."""
    body = _CI.partition(f"\n{key}:\n")[2]
    assert body, f"`{key.strip()}:` is no longer a block mapping on a line of its own"

    deeper = " " * (len(key) - len(key.lstrip(" ")) + 1)
    lines = []
    for line in body.splitlines():
        if line and not line.startswith(deeper):
            break
        lines.append(line)
    return "\n".join(lines)


def _job_ids() -> set[str]:
    """Every job id in the workflow, by its two-space indent under `jobs:`."""
    return set(_BLOCK_KEY.findall(_block("jobs")))


def _triggers() -> set[str]:
    """Every event in the workflow's `on:` block, by its two-space indent."""
    return set(_BLOCK_KEY.findall(_block("on")))


def _job_block(job_id: str) -> str:
    """One job's own lines — those indented deeper than its key."""
    return _block(f"  {job_id}")


class AggregateGateTest(unittest.TestCase):
    def test_the_gate_needs_every_other_job(self):
        needs = _INLINE_NEEDS.search(_job_block(_GATE))
        assert needs is not None, f"the `{_GATE}` job's `needs:` is no longer an inline list"
        self.assertEqual(
            _job_ids() - {_GATE},
            {name.strip() for name in needs.group(1).split(",")},
            "a job outside the gate's `needs:` can go red without blocking a merge",
        )

    def test_the_gate_runs_on_a_merge_group(self):
        self.assertIn(
            "merge_group",
            _triggers(),
            "a queued pull request builds a merge group this workflow ignores, "
            "so the gate never reports and the queue evicts the entry on timeout",
        )

    def test_the_gate_reports_when_a_dependency_fails(self):
        self.assertIn(
            "always()",
            _job_block(_GATE),
            "without `always()` the gate skips, and a skipped check satisfies branch protection",
        )
