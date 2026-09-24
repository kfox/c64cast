"""Guards for how this repository pins and configures its Node toolchain.

`.node-version` is the one place the Node version is written. Every reader of
it resolves the same number — `actions/setup-node` through `node-version-file`,
and mise through the idiomatic-version-file setting in `mise.toml` — so a
second statement of it anywhere is a statement that can disagree, and the
disagreement surfaces as a byte diff in the committed `c64cast/web/dist`.

`web/.npmrc` is where install lifecycle scripts are turned off, rather than a
`--ignore-scripts` flag on one invocation: it is the only form that also covers
the bare `npm install` a contributor types, and npm reads it for `npm ci` in CI
just the same.

npm applies that one switch to `npm run` as well — it runs the named script but
silently skips its `pre`/`post` hook — so a hook added to `web/package.json`
would stop running without a word. That is the failure this module makes noisy.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NPMRC = os.path.join(_REPO, "web", ".npmrc")
_PACKAGE_JSON = os.path.join(_REPO, "web", "package.json")
_NODE_VERSION = os.path.join(_REPO, ".node-version")
_MISE = os.path.join(_REPO, "mise.toml")
_WORKFLOWS = os.path.join(_REPO, ".github", "workflows")

_PIN_FILE = ".node-version"
_VERSION_SPEC = re.compile(r"\A\d+(\.\d+){0,2}\Z")
_STEP_START = re.compile(r"([ \t]*)-(?:[ \t]|$)")
_SETUP_NODE = re.compile(r"^[ \t]*(?:-[ \t]+)?uses:[ \t]*actions/setup-node@", re.M)
_STATES_A_VERSION = re.compile(r"^\s*node-version:\s*(\S.*?)\s*$", re.M)
_READS_A_VERSION_FILE = re.compile(r"^[ \t]*node-version-file:[ \t]*(\S+)", re.M)

# The hooks npm fires on its own, with no `npm run` naming them. The second
# group hangs off npm's built-in commands (`npm start`, `npm test`, `npm stop`,
# `npm restart`, `npm version`, `npm publish`): npm still runs the command's own
# script under `ignore-scripts`, but drops these, and it does so whether or not
# `web/package.json` declares the script they wrap.
_SELF_FIRING_HOOKS = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "preprepare",
        "prepare",
        "postprepare",
        "prepack",
        "postpack",
        "prepublish",
        "prepublishOnly",
        "dependencies",
        "prestart",
        "poststart",
        "pretest",
        "posttest",
        "prestop",
        "poststop",
        "prerestart",
        "postrestart",
        "preversion",
        "version",
        "postversion",
        "publish",
        "postpublish",
    }
)

_TRUE = frozenset({"true", "1", "yes", "on"})


def _npmrc() -> dict[str, str]:
    """`web/.npmrc` as a mapping, ignoring blank lines and comments.

    A line with no `=` is a key on its own, which npm's ini reader takes as
    `true` — read it the same way rather than calling the file malformed.
    """
    settings: dict[str, str] = {}
    with open(_NPMRC, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line[0] in ";#":
                continue
            key, sep, value = line.partition("=")
            settings[key.strip()] = value.strip() if sep else "true"
    return settings


def _scripts() -> dict[str, str]:
    with open(_PACKAGE_JSON, encoding="utf-8") as handle:
        return dict(json.load(handle).get("scripts", {}))


def _workflows() -> dict[str, str]:
    """Every workflow file under `.github/workflows`, by name."""
    names = [name for name in os.listdir(_WORKFLOWS) if name.endswith((".yml", ".yaml"))]
    assert names, "no workflow files found — this module is reading the wrong directory"
    sources = {}
    for name in names:
        with open(os.path.join(_WORKFLOWS, name), encoding="utf-8") as handle:
            sources[name] = handle.read()
    return sources


def _setup_node_steps(workflow: str) -> list[str]:
    """Each `actions/setup-node` step, in either shape a step can be written in.

    A step is sliced from its `-` line to the next line at or left of that
    line's indent, then kept if `actions/setup-node` appears anywhere in it.
    Matching the `uses:` line instead would miss the `- name:` form — `uses:`
    then sits a line below the `-`, and a slice starting there would end at the
    sibling `with:` that carries `node-version-file`.
    """
    lines = workflow.splitlines()
    steps = []
    for start, line in enumerate(lines):
        match = _STEP_START.match(line)
        if match is None:
            continue
        indent = len(match.group(1))
        end = start + 1
        while end < len(lines):
            text = lines[end]
            if text.strip() and len(text) - len(text.lstrip()) <= indent:
                break
            end += 1
        step = "\n".join(lines[start:end])
        if _SETUP_NODE.search(step):
            steps.append(step)
    return steps


def _version_files(step: str) -> list[str]:
    """The `node-version-file:` values in a step, unquoted."""
    return [value.strip("\"'") for value in _READS_A_VERSION_FILE.findall(step)]


class InstallScriptsTest(unittest.TestCase):
    def test_install_lifecycle_scripts_are_off(self):
        self.assertIn(
            _npmrc().get("ignore-scripts", "").lower(),
            _TRUE,
            "without ignore-scripts, every dependency's preinstall/install/postinstall "
            "runs on `npm ci` in CI and on a contributor's `npm install`",
        )


class LifecycleHookTest(unittest.TestCase):
    """`ignore-scripts` drops `pre`/`post` hooks, so `web/` must declare none."""

    def test_no_script_is_another_scripts_hook(self):
        names = set(_scripts())
        hooks = {f"{prefix}{name}" for name in names for prefix in ("pre", "post")}
        self.assertEqual(
            set(),
            hooks & names,
            "web/.npmrc's ignore-scripts makes `npm run` skip these, so they never run",
        )

    def test_no_script_is_one_npm_fires_itself(self):
        self.assertEqual(
            set(),
            set(_scripts()) & _SELF_FIRING_HOOKS,
            "web/.npmrc's ignore-scripts makes npm skip these, so they never run",
        )


class NodeVersionPinTest(unittest.TestCase):
    def test_the_pin_is_one_version_spec(self):
        with open(_NODE_VERSION, encoding="utf-8") as handle:
            pinned = handle.read().strip()
        self.assertRegex(
            pinned,
            _VERSION_SPEC,
            f"{_PIN_FILE} holds a plain version like `24`; an alias such as `lts/*` "
            "resolves per reader, which is the disagreement this file exists to remove",
        )

    def test_no_workflow_states_a_node_version_of_its_own(self):
        for name, source in _workflows().items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    [],
                    _STATES_A_VERSION.findall(source),
                    f"a version written here can disagree with {_PIN_FILE}; "
                    "use `node-version-file` instead",
                )

    def test_every_setup_node_reads_the_pin_file(self):
        for name, source in _workflows().items():
            for step in _setup_node_steps(source):
                with self.subTest(workflow=name, step=step.splitlines()[0].strip()):
                    self.assertEqual(
                        [_PIN_FILE],
                        _version_files(step),
                        "a setup-node step naming no version file installs whatever "
                        "Node the runner image happens to ship",
                    )

    def test_mise_resolves_the_same_file(self):
        with open(_MISE, "rb") as handle:
            settings = tomllib.load(handle).get("settings", {})
        self.assertIn(
            "node",
            settings.get("idiomatic_version_file_enable_tools", []),
            f"without this mise ignores {_PIN_FILE}, so a local `make web` builds "
            "the committed bundle on a different Node than CI rebuilds it on",
        )


class SetupNodeStepReadingTest(unittest.TestCase):
    """`_setup_node_steps` against the step shapes a workflow can put it in."""

    def test_a_step_ends_at_the_next_step(self):
        workflow = (
            "      - uses: actions/setup-node@abc\n"
            "        with:\n"
            "          node-version-file: .node-version\n"
            "      - uses: actions/cache@def\n"
            "        with:\n"
            "          node-version: '24'\n"
        )
        self.assertEqual([".node-version"], _version_files(_setup_node_steps(workflow)[0]))

    def test_a_last_step_ends_at_the_next_dedented_key(self):
        workflow = (
            "  docs:\n"
            "    steps:\n"
            "      - uses: actions/setup-node@abc\n"
            "  web:\n"
            "    steps:\n"
            "      - uses: actions/setup-node@abc\n"
            "        with:\n"
            "          node-version-file: .node-version\n"
        )
        first, second = _setup_node_steps(workflow)
        self.assertEqual([], _version_files(first))
        self.assertEqual([".node-version"], _version_files(second))

    def test_a_blank_line_inside_a_step_does_not_end_it(self):
        workflow = (
            "      - uses: actions/setup-node@abc\n"
            "\n"
            "        with:\n"
            "          node-version-file: .node-version\n"
        )
        self.assertEqual([".node-version"], _version_files(_setup_node_steps(workflow)[0]))

    def test_a_step_named_before_its_uses_line_is_still_a_step(self):
        workflow = (
            "      - name: Set up Node\n"
            "        uses: actions/setup-node@abc\n"
            "        with:\n"
            "          node-version-file: .node-version\n"
            "      - run: node --test x.mjs\n"
        )
        self.assertEqual([".node-version"], _version_files(_setup_node_steps(workflow)[0]))

    def test_a_named_step_reading_no_version_file_is_still_seen(self):
        workflow = "      - name: Set up Node\n        uses: actions/setup-node@abc\n"
        self.assertEqual([[]], [_version_files(s) for s in _setup_node_steps(workflow)])

    def test_a_quoted_version_file_is_the_file_it_names(self):
        self.assertEqual([".node-version"], _version_files("  node-version-file: '.node-version'"))

    def test_a_trailing_comment_is_not_part_of_the_version_file(self):
        self.assertEqual(
            [".node-version"], _version_files("  node-version-file: .node-version  # pinned")
        )

    def test_node_version_file_is_not_read_as_a_stated_version(self):
        self.assertEqual([], _STATES_A_VERSION.findall("          node-version-file: x\n"))
