"""Guards for .github/workflows/ci.yml.

Branch protection requires the gate's context in place of one per matrix leg,
so its `needs:` list is what decides which jobs can block a merge, and
`if: always()` is what makes it report — GitHub counts a skipped job as a pass.

A merge queue gates on that same context, evaluated against the merge group it
builds, which is why the workflow triggers on `merge_group` too: a required
check that never reports there does not fail the entry, it holds the queue
until the status-check timeout evicts it.

The `use_oidc` input and an `id-token: write` permission have to travel
together: without the permission the action's token step throws before the
upload runs, and the error it raises names neither of them.
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
_USE_OIDC = re.compile(r"^\s+use_oidc:\s*(.+?)\s*$", re.M)
_JOB_PERMISSIONS = re.compile(r"^    permissions:(.*)\n((?:(?:     .*)?\n)*)", re.M)
_WORKFLOW_PERMISSIONS = re.compile(r"^permissions:(.*)\n((?:(?: .*)?\n)*)", re.M)
_COMMENT = re.compile(r"#.*")
_OIDC_PERMISSION = re.compile(r"id-token:\s*[\"']?write")

_GATE = "ci"
_EVERY_PERMISSION = "write-all"


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
    return "".join(f"{line}\n" for line in lines)


def _job_ids() -> set[str]:
    """Every job id in the workflow, by its two-space indent under `jobs:`."""
    return set(_BLOCK_KEY.findall(_block("jobs")))


def _triggers() -> set[str]:
    """Every event in the workflow's `on:` block, by its two-space indent."""
    return set(_BLOCK_KEY.findall(_block("on")))


def _job_block(job_id: str) -> str:
    """One job's own lines — those indented deeper than its key."""
    return _block(f"  {job_id}")


def _asks_for_oidc(job_block: str) -> bool:
    """Whether a job uploads with OIDC — a value this cannot read counts as yes."""
    values = (match.group(1).strip("\"'").lower() for match in _USE_OIDC.finditer(job_block))
    return any(value != "false" for value in values)


def _grants_oidc(job_block: str, workflow: str = _CI) -> bool:
    """Whether the permissions a job runs under let it mint an OIDC token.

    A job's `permissions:` replaces the workflow-level block rather than merging
    with it — an unlisted scope "is set to none" — so only the block that wins
    counts, and `id-token` is in neither default.
    """
    match = _JOB_PERMISSIONS.search(job_block) or _WORKFLOW_PERMISSIONS.search(workflow)
    granted = _COMMENT.sub("", "\n".join(match.groups())) if match else ""
    return bool(_OIDC_PERMISSION.search(granted)) or granted.strip(" \n\"'") == _EVERY_PERMISSION


_WORKFLOW_GRANTS = "permissions:\n  id-token: write\n"
_WORKFLOW_SILENT = "permissions:\n  contents: read\n"


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


class OidcUploadTest(unittest.TestCase):
    def test_a_job_uploading_with_oidc_can_mint_its_token(self):
        for job_id in _job_ids():
            block = _job_block(job_id)
            if not _asks_for_oidc(block):
                continue
            self.assertTrue(
                _grants_oidc(block),
                f"the `{job_id}` job uploads with OIDC but cannot mint a token, "
                "so the action's token step throws before the upload runs",
            )


class PermissionReadingTest(unittest.TestCase):
    """`_grants_oidc` against the spellings GitHub accepts for the same grant."""

    def test_a_block_ends_with_a_newline_so_its_last_line_is_read(self):
        self.assertTrue(_block(f"  {_GATE}").endswith("\n"))

    def test_a_jobs_own_block_replaces_a_granting_workflow(self):
        self.assertFalse(
            _grants_oidc("    permissions: read-all\n", _WORKFLOW_GRANTS),
            "a job that overrides the grant away cannot mint a token the workflow grants",
        )

    def test_a_job_declaring_no_permissions_runs_under_the_workflow_block(self):
        self.assertTrue(_grants_oidc("    runs-on: ubuntu-latest\n", _WORKFLOW_GRANTS))

    def test_a_commented_out_grant_is_not_a_grant(self):
        self.assertFalse(
            _grants_oidc("    permissions:\n      # id-token: write\n", _WORKFLOW_GRANTS),
            "a grant someone commented out still reads as granted",
        )

    def test_a_comment_on_the_key_does_not_hide_the_grant(self):
        job = "    permissions: # least privilege\n      id-token: write\n"
        self.assertTrue(_grants_oidc(job, _WORKFLOW_SILENT))

    def test_a_grant_indented_one_space_deeper_than_its_key_counts(self):
        job = "    permissions:\n     id-token: write\n"
        self.assertTrue(_grants_oidc(job, _WORKFLOW_SILENT))

    def test_a_quoted_or_padded_grant_counts(self):
        for value in ("write", "'write'", '"write"', "  write"):
            with self.subTest(value=value):
                job = f"    permissions:\n      id-token: {value}\n"
                self.assertTrue(_grants_oidc(job, _WORKFLOW_SILENT))

    def test_write_all_counts_however_it_is_spelled(self):
        for value in ("write-all", "write-all # everything", '"write-all"'):
            with self.subTest(value=value):
                self.assertTrue(_grants_oidc(f"    permissions: {value}\n", _WORKFLOW_SILENT))

    def test_read_all_is_not_a_grant(self):
        self.assertFalse(_grants_oidc("    permissions: read-all\n", _WORKFLOW_SILENT))
