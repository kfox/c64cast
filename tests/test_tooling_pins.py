"""A dev tool's version is written in pyproject.toml and nowhere else.

`.pre-commit-config.yaml` runs ruff, pyright and the suite out of the project
environment rather than re-pinning them, so the commit hook, CI and `make lint`
cannot resolve different builds
(https://github.com/kfox/c64cast/issues/398).
"""

from __future__ import annotations

import os
import re
import tomllib
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_REPO, "pyproject.toml")
_PRECOMMIT = os.path.join(_REPO, ".pre-commit-config.yaml")

_PROJECT_ENV = "uv run --locked"

_BLOCK = re.compile(r"^[ \t]*-[ \t]*repo:.*?(?=^[ \t]*-[ \t]*repo:|\Z)", re.M | re.S)
_HOOK = re.compile(r"^[ \t]*-[ \t]*id:.*?(?=^[ \t]*-[ \t]*id:|\Z)", re.M | re.S)
_REPO_URL = re.compile(r"^[ \t]*-[ \t]*repo:[ \t]*(\S+?)[ \t]*(?:#.*)?$", re.M)
_REPO_KEY = re.compile(r"\brepo[ \t]*:")
_REV = re.compile(r"^[ \t]*rev:[ \t]*(\S+?)[ \t]*(?:#.*)?$", re.M)
_HOOK_ID = re.compile(r"^[ \t]*-[ \t]*id:[ \t]*(\S+?)[ \t]*(?:#.*)?$", re.M)
_HOOK_KEY = re.compile(r"\bid[ \t]*:")
_ENTRY = re.compile(r"^[ \t]*entry:[ \t]*(.+?)[ \t]*(?:#.*)?$", re.M)
_EXACT_PIN = re.compile(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s,;]+)\s*(?:;.*)?")
_ADDL_DEPS = re.compile(
    r"^(?P<indent>[ \t]*)additional_dependencies:(?P<value>.*(?:\n(?P=indent)[ \t]+.*)*)",
    re.M,
)
_WORD = re.compile(r"[A-Za-z0-9._-]+")
_COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.M)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _keys(pattern: re.Pattern[str], text: str) -> list[str]:
    """Every occurrence of `pattern` in `text` outside a whole-line comment."""
    return pattern.findall(_COMMENT_LINE.sub("", text))


def _canonical(name: str) -> str:
    """A distribution name in the one spelling PEP 503 compares."""
    return re.sub(r"[-_.]+", "-", name.strip("\"'")).lower()


def _pinned_tools() -> dict[str, str]:
    """Every distribution pyproject pins exactly, across all dependency groups."""
    with open(_PYPROJECT, "rb") as f:
        groups = tomllib.load(f)["dependency-groups"]
    specs = (s for group in groups.values() for s in group if isinstance(s, str))
    matched = (_EXACT_PIN.fullmatch(spec) for spec in specs)
    return {_canonical(m[1]): m[2] for m in matched if m}


def _version_sources(raw: str) -> set[str]:
    """Every distribution .pre-commit-config.yaml would install a build of.

    An `additional_dependencies` entry counts whatever its specifier says:
    a bare name floats and a range resolves, so both install a build beside
    the one pyproject pins.
    """
    exact = {_canonical(m[1]) for m in _EXACT_PIN.finditer(raw)}
    declared = {
        _canonical(word) for m in _ADDL_DEPS.finditer(raw) for word in _WORD.findall(m["value"])
    }
    return exact | declared


def _blocks() -> list[str]:
    """Each `- repo:` block of .pre-commit-config.yaml, in order.

    Counted against the raw text by the caller rather than trusted: a block the
    pattern fails to see is not unpinned, it is unexamined, which reads back
    exactly like a pass.
    """
    return _BLOCK.findall(_read(_PRECOMMIT))


def _mirrored_names(block: str, repo_url: str) -> set[str]:
    """Every distribution the pre-commit repo in `block` could be re-pinning.

    A mirror names its distribution in the repo slug by pre-commit's own
    convention -- `ruff-pre-commit` -> `ruff`, `mirrors-mypy` -> `mypy`,
    `black-pre-commit-mirror` -> `black` -- and its hook ids usually name it
    too, which is the half that catches `RobertCraigie/pyright-python`.
    """
    slug = _canonical(repo_url.rstrip("/").rsplit("/", 1)[-1])
    mirrored = slug.removeprefix("mirrors-").removesuffix("-mirror").removesuffix("-pre-commit")
    return {mirrored, *(_canonical(hook) for hook in _HOOK_ID.findall(block))}


class SinglePinTest(unittest.TestCase):
    def test_no_pre_commit_repo_re_pins_a_tool_pyproject_pins(self) -> None:
        raw = _read(_PRECOMMIT)
        blocks = _blocks()
        self.assertEqual(
            len(blocks),
            len(_keys(_REPO_KEY, raw)),
            "a `repo:` block was not parsed, so nothing checked what it pins",
        )

        tools = _pinned_tools()
        for tool in ("ruff", "pyright", "mypy"):
            self.assertIn(tool, tools, f"pyproject stopped pinning {tool} exactly")

        repinned = sorted(_version_sources(raw) & tools.keys())
        self.assertFalse(
            repinned,
            f".pre-commit-config.yaml installs {repinned} itself — a second "
            f"build beside pyproject's. Run the tool from the project "
            f"environment with a `{_PROJECT_ENV}` entry instead, so there is "
            f"one version",
        )

        for block in blocks:
            url, rev = _REPO_URL.search(block), _REV.search(block)
            if url is None or rev is None:
                continue
            clash = sorted(_mirrored_names(block, url[1]) & tools.keys())
            self.assertFalse(
                clash,
                f"{url[1]} pins {rev[1]}, a second version for {clash} — which "
                f"pyproject already pins. Run it from the project environment "
                f"with a `{_PROJECT_ENV}` entry instead, so there is one version",
            )


class ProjectEnvironmentTest(unittest.TestCase):
    def test_every_local_hook_runs_through_the_project_environment(self) -> None:
        local = [b for b in _blocks() if (m := _REPO_URL.search(b)) and m[1] == "local"]
        self.assertEqual(len(local), 1, "expected exactly one `- repo: local` block")

        hooks = _HOOK.findall(local[0])
        self.assertEqual(
            len(hooks),
            len(_keys(_HOOK_KEY, local[0])),
            "a local hook was not parsed, so nothing checked how it resolves",
        )
        self.assertTrue(hooks, "the local block declares no hooks")

        for hook in hooks:
            name = _HOOK_ID.search(hook)
            entry = _ENTRY.search(hook)
            self.assertIsNotNone(name, f"a local hook has no id: {hook!r}")
            assert name is not None
            self.assertIsNotNone(entry, f"local hook {name[1]} has no entry")
            assert entry is not None
            self.assertTrue(
                entry[1].startswith(_PROJECT_ENV),
                f"local hook {name[1]} runs `{entry[1]}`, which can resolve a "
                f"different build than `make lint` does: a bare entry takes "
                f"whatever is on PATH, `--frozen` takes a uv.lock that may no "
                f"longer match pyproject.toml, and a bare `uv run` can rewrite "
                f"the lock mid-commit. Prefix it with `{_PROJECT_ENV}`",
            )
