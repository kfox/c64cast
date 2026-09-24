"""The lift signal for the `typescript >= 7` hold in `.github/dependabot.yml`.

The hold exists for one reason: svelte-check peers on `typescript ^5 || ^6`,
and a grouped bump past that makes `npm ci` fail ERESOLVE for every update in
the npm group. When svelte-check ships a release that accepts 7 the reason is
gone, but nothing about Dependabot notices — the entry would keep the web
console on TypeScript 6 until a person happened to re-check by hand.

`web/package-lock.json` records the peer range of the svelte-check actually
installed, so the weekly bump that widens it is also what turns this red. The
only way back to green is deleting the `ignore` block, which is the decision
the hold was deferring. Whether to then take TypeScript 7 is a separate one,
made on the Dependabot pull request that proposes it.
"""

from __future__ import annotations

import json
import os
import re
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEPENDABOT = os.path.join(_REPO, ".github", "dependabot.yml")
_LOCKFILE = os.path.join(_REPO, "web", "package-lock.json")

_HELD = "typescript"
_HELD_MAJOR = 7
_PEERED_ON_BY = "node_modules/svelte-check"

_IGNORE_ENTRY = re.compile(
    r"\A(?P<indent>\s*)-\s+dependency-name:\s*[\"']?(?P<name>[^\"'\s]+)[\"']?\s*\Z"
)
_VERSIONS_KEY = re.compile(r"\A\s*versions:\s*\Z")
_LIST_ITEM = re.compile(r"\A\s*-\s*[\"']?(?P<value>[^\"'\n]+?)[\"']?\s*\Z")
_COMPARATOR = re.compile(r"\A(?P<op>\^|~|>=|=)?(?P<major>\d+)(?:\.[\w.+-]*)?\Z")
# `>= 7.0.0` is one comparator; the space left in it is not the AND that
# separates two, as in `>=5.0.0 <8.0.0`.
_PADDED_OPERATOR = re.compile(r"([<>=~^]+)\s+")


def _lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def _indented_past(lines: list[str], start: int, indent: int) -> list[str]:
    """The lines after `start`, up to the first one not indented past `indent`."""
    block = []
    for text in lines[start + 1 :]:
        if text.strip() and len(text) - len(text.lstrip()) <= indent:
            break
        block.append(text)
    return block


def _ignored_versions(name: str, lines: list[str] | None = None) -> list[str]:
    """The specs every `ignore:` entry for `name` lists under `versions:`."""
    lines = _lines(_DEPENDABOT) if lines is None else lines
    specs = []
    for start, line in enumerate(lines):
        entry = _IGNORE_ENTRY.match(line)
        if entry is None or entry.group("name") != name:
            continue
        body = _indented_past(lines, start, len(entry.group("indent")))
        for offset, text in enumerate(body):
            if _VERSIONS_KEY.match(text) is None:
                continue
            for item in _indented_past(body, offset, len(text) - len(text.lstrip())):
                match = _LIST_ITEM.match(item)
                assert match is not None, f"{item!r} is not a `versions:` list item"
                specs.append(match.group("value"))
    return specs


def _comparator_admits(text: str, major: int) -> bool:
    match = _COMPARATOR.match(text)
    assert match is not None, (
        f"{text!r} is a comparator this module cannot read — extend it, and while "
        f"you are here re-check whether the {_HELD} hold can be lifted"
    )
    operator, bound = match.group("op") or "=", int(match.group("major"))
    if operator == ">=":
        return major >= bound
    return major == bound


def _admits_major(spec: str, major: int) -> bool:
    """Whether a semver range lets through any version with this major."""
    clauses = [_PADDED_OPERATOR.sub(r"\1", clause).split() for clause in spec.split("||")]
    return any(
        all(_comparator_admits(text, major) for text in comparators)
        for comparators in clauses
        if comparators
    )


def _peer_range() -> str | None:
    """svelte-check's recorded `typescript` peer range, or None if it is gone."""
    with open(_LOCKFILE, encoding="utf-8") as handle:
        packages = json.load(handle)["packages"]
    return packages.get(_PEERED_ON_BY, {}).get("peerDependencies", {}).get(_HELD)


class TypescriptHoldTest(unittest.TestCase):
    def test_the_hold_lasts_exactly_as_long_as_its_reason(self):
        held = any(_admits_major(spec, _HELD_MAJOR) for spec in _ignored_versions(_HELD))
        peer = _peer_range()

        if peer is None:
            self.assertFalse(
                held,
                f"nothing in web/package-lock.json peers on {_HELD} any more, so the "
                "ignore entry in .github/dependabot.yml is holding back nothing",
            )
        elif _admits_major(peer, _HELD_MAJOR):
            self.assertFalse(
                held,
                f"svelte-check now peers on `{peer}`, so the hold can go: delete the "
                f"{_HELD} ignore entry in .github/dependabot.yml",
            )
        else:
            self.assertTrue(
                held,
                f"svelte-check peers on `{peer}`, so a grouped bump to {_HELD} "
                f"{_HELD_MAJOR} makes `npm ci` fail ERESOLVE for the whole npm group",
            )


class RangeReadingTest(unittest.TestCase):
    """`_admits_major` against the range shapes npm writes."""

    def test_a_disjunction_admits_each_of_its_majors(self):
        for major, expected in ((4, False), (5, True), (6, True), (7, False)):
            with self.subTest(major=major):
                self.assertIs(expected, _admits_major("^5.0.0 || ^6.0.0", major))

    def test_a_widened_disjunction_admits_the_new_major(self):
        self.assertTrue(_admits_major("^5.0.0 || ^6.0.0 || ^7.0.0", 7))

    def test_an_open_lower_bound_admits_everything_above_it(self):
        self.assertTrue(_admits_major(">=6.0.0", 7))

    def test_the_holds_own_spelling_covers_the_major_it_names(self):
        self.assertTrue(_admits_major(">= 7.0.0", 7))

    def test_a_tilde_range_admits_only_its_own_major(self):
        self.assertTrue(_admits_major("~7.1.0", 7))
        self.assertFalse(_admits_major("~7.1.0", 8))

    def test_a_bare_version_admits_only_itself(self):
        self.assertTrue(_admits_major("6.0.3", 6))
        self.assertFalse(_admits_major("6.0.3", 7))

    def test_a_shape_this_cannot_read_raises_instead_of_answering(self):
        with self.assertRaises(AssertionError):
            _admits_major(">=5.0.0 <8.0.0", 7)


class IgnoreEntryReadingTest(unittest.TestCase):
    """`_ignored_versions` against the committed file and its neighbors."""

    def test_the_committed_hold_is_found(self):
        self.assertTrue(
            any(_admits_major(spec, _HELD_MAJOR) for spec in _ignored_versions(_HELD)),
            "the reader stopped finding the entry it exists to watch",
        )

    def test_a_name_with_no_entry_has_no_specs(self):
        self.assertEqual([], _ignored_versions("svelte-check"))

    def test_only_the_named_entrys_versions_are_collected(self):
        lines = [
            "    ignore:",
            "      - dependency-name: typescript",
            "        versions:",
            '          - ">= 7.0.0"',
            "      - dependency-name: svelte",
            "        versions:",
            '          - ">= 6.0.0"',
        ]
        self.assertEqual([">= 7.0.0"], _ignored_versions("typescript", lines))
        self.assertEqual([">= 6.0.0"], _ignored_versions("svelte", lines))

    def test_a_sibling_key_in_the_entry_is_not_read_as_a_version(self):
        lines = [
            "      - dependency-name: typescript",
            "        update-types:",
            '          - "version-update:semver-major"',
            "        versions:",
            '          - ">= 7.0.0"',
        ]
        self.assertEqual([">= 7.0.0"], _ignored_versions("typescript", lines))
