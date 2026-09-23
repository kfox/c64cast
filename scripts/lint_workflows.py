#!/usr/bin/env python3
"""Check that a GitHub workflow's `needs:` graph names jobs that exist.

GitHub resolves `needs:` when it dispatches the run, which is after the push.
Until then a name matching no job costs nothing: the file is still well-formed
YAML, so `check-yaml` passes, and no test in this repository has an opinion
about it. The instance that prompted this was `needs: tset` for `needs: test`,
which silently ungates whatever the misspelled job was there to gate
(https://github.com/kfox/c64cast/issues/446).

Four findings, all the same shape -- a cross-reference GitHub only resolves at
dispatch, where being wrong costs a round trip:

- a `needs:` entry naming a job the workflow does not declare
- a `needs:` that is neither a job id nor a list of them, which means the
  dependencies were not read and the graph below is not the real one
- `${{ needs.<job>... }}` (or a bare `if:` expression) in a job that does not
  declare `<job>` in its `needs:`. GitHub evaluates that to the empty string
  rather than failing, so the step runs with a blank where a version was
- a cycle in the `needs:` graph, which leaves the run undispatched

Reading a workflow with a parser rather than a regex is also what lets a guard
ask which *job* holds a permission, so this doubles as the repository's
workflow reader: `tests/test_ci_workflow.py` and `tests/test_release.py`
resolve effective permissions through `effective_permissions` here instead of
each matching the raw text against its own idea of the layout
(https://github.com/kfox/c64cast/issues/471).
"""

from __future__ import annotations

import graphlib
import os
import re
import sys
from collections.abc import Iterator
from typing import Any

import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(_REPO, ".github", "workflows")
WORKFLOW_SUFFIXES = (".yml", ".yaml")

_OIDC_SCOPE = "id-token"
_WRITE = "write"
_EVERY_PERMISSION = "write-all"

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_NEEDS_REF = re.compile(r"\bneeds\.([A-Za-z_][A-Za-z0-9_-]*)")


def parse(text: str) -> Any:
    """One workflow, as YAML. `yaml.YAMLError` if it is not."""
    return yaml.safe_load(text)


def load(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def jobs(workflow: Any) -> dict[str, Any]:
    """The workflow's `jobs:` mapping, empty if it declares none."""
    declared = workflow.get("jobs") if isinstance(workflow, dict) else None
    return declared if isinstance(declared, dict) else {}


def needs_of(job: Any) -> list[str] | None:
    """The job ids a job declares in `needs:`, or None if the shape is unreadable.

    `needs:` takes a single job id or a list of them, and both spellings appear
    in this repository.
    """
    declared = job.get("needs") if isinstance(job, dict) else None
    if declared is None:
        return []
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list) and all(isinstance(item, str) for item in declared):
        return list(declared)
    return None


def effective_permissions(workflow: Any, job: Any) -> Any:
    """The `permissions:` block a job actually runs under.

    A job's own block replaces the workflow-level one rather than merging with
    it -- an unlisted scope "is set to none" -- so the block that wins is the
    only one worth reading. Neither default carries `id-token`.
    """
    for block in (job, workflow):
        if isinstance(block, dict) and "permissions" in block:
            return block["permissions"]
    return None


def grants_oidc(permissions: Any) -> bool:
    """Whether those permissions let a job mint an OIDC token.

    A shape this cannot read counts as no grant, so an unreadable block fails
    the guard that calls this rather than satisfying it.
    """
    if isinstance(permissions, str):
        return permissions.strip() == _EVERY_PERMISSION
    if isinstance(permissions, dict):
        return str(permissions.get(_OIDC_SCOPE, "")).strip() == _WRITE
    return False


def _expressions(node: Any, is_condition: bool = False) -> Iterator[str]:
    """Every expression in a job: `${{ ... }}` spans, plus whole `if:` values.

    An `if:` is an expression whether or not it is wrapped, so its entire value
    counts; everywhere else only what the braces enclose does.
    """
    if isinstance(node, str):
        if is_condition:
            yield node
        yield from _EXPRESSION.findall(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _expressions(value, is_condition=key == "if")
    elif isinstance(node, list):
        for item in node:
            yield from _expressions(item)


def referenced_needs(job: Any) -> list[str]:
    """Every job id a job's expressions read through `needs.<id>`."""
    found = {name for text in _expressions(job) for name in _NEEDS_REF.findall(text)}
    return sorted(found)


def problems(name: str, workflow: Any) -> list[str]:
    """Everything wrong with one parsed workflow's `needs:` graph."""
    declared = jobs(workflow)
    if not declared:
        return [f"{name}: declares no `jobs:` mapping, so nothing here was checked"]

    found: list[str] = []
    graph: dict[str, list[str]] = {}
    for job_id, job in declared.items():
        needs = needs_of(job)
        if needs is None:
            found.append(
                f"{name}: job `{job_id}` has a `needs:` that is neither a job id "
                f"nor a list of them, so its dependencies cannot be read"
            )
            needs = []

        for dependency in needs:
            if dependency not in declared:
                found.append(
                    f"{name}: job `{job_id}` needs `{dependency}`, which is not a "
                    f"job in this workflow"
                )

        for reference in referenced_needs(job):
            if reference not in needs:
                found.append(
                    f"{name}: job `{job_id}` reads `needs.{reference}` without "
                    f"declaring `{reference}` in its `needs:`, so GitHub "
                    f"substitutes the empty string"
                )

        graph[job_id] = [dependency for dependency in needs if dependency in declared]

    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        found.append(f"{name}: the `needs:` graph has a cycle: {' -> '.join(exc.args[1])}")

    return found


def file_problems(path: str) -> list[str]:
    """Everything wrong with the workflow at `path`, including not being YAML."""
    name = os.path.basename(path)
    try:
        workflow = load(path)
    except yaml.YAMLError as exc:
        return [f"{name}: does not parse as YAML: {exc}"]
    return problems(name, workflow)


def workflow_paths(directory: str | None = None) -> list[str]:
    """Every workflow file in `directory`, defaulting to this repository's.

    Resolved when called rather than bound as a default, so the directory a
    test points this at is the one that gets read.
    """
    directory = directory or WORKFLOW_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name) for name in names if name.endswith(WORKFLOW_SUFFIXES)
    )


def main(argv: list[str]) -> int:
    paths = argv[1:] or workflow_paths()
    if not paths:
        print(
            f"no workflow files under {WORKFLOW_DIR} — nothing was checked, so this is not a pass",
            file=sys.stderr,
        )
        return 1

    found = [problem for path in paths for problem in file_problems(path)]
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        print(
            "\nGitHub resolves a `needs:` when it dispatches the run, so these "
            "cost a push to find out about.",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
