"""Guards for how this repository pins and configures its Node toolchain.

`.node-version` is the one place the Node version is written. Every reader of
it resolves the same number — `actions/setup-node` through `node-version-file`,
and mise through the idiomatic-version-file setting in `mise.toml` — so a
second statement of it anywhere is a statement that can disagree.

A disagreement is silent, which is why the pin is guarded here rather than
left to CI to notice. Node 24 and Node 26 build a byte-identical
`c64cast/web/dist`, so `git diff --exit-code` on the committed bundle stays
green across a Node major — and the three-way split this file closed, between
CI's `web` job, CI's `docs` job on the runner image's default, and a local
mise, ran that way unnoticed.

`web/.npmrc` is where install lifecycle scripts are turned off, rather than a
`--ignore-scripts` flag on one invocation: it is the only form that also covers
the bare `npm install` a contributor types, and npm reads it for `npm ci` in CI
just the same.

npm applies that one switch to `npm run` as well — it runs the named script but
silently skips its `pre`/`post` hook — so a hook added to `web/package.json`
would stop running without a word. That is the failure this module makes noisy.

The scripts the switch is actually aimed at are the dependencies', and
`web/package-lock.json` records which package has one. A package that needs
its install script to put a binary in place installs clean under the switch
and fails later, in `vite build`, naming neither the script nor the setting —
so a new one has to be read before it is skipped.

`build.target` in `web/vite.config.ts` is the console's browser floor, and it
is guarded for the same reason the Node pin is: left unstated it is Vite's
`baseline-widely-available` default, which resolves to a later set of browsers
with each Vite version. The bundle diff CI runs cannot tell that move from an
ordinary re-minification, and deleting the stated value does not move the
bundle at all for as long as the default still matches it — so the rebuild is
green either way, and this is the only reader that fails.

The target is read out of the config as text, there being no TypeScript parser
here to read it with. Three shapes make that reading narrower than a search for
`target:`. The dev-server proxy entries each have a `target` of their own and
an option nested under `build` may have one too, so the read is scoped to the
`build` block's own keys. A commented-out line is still a line, so comments
come out first. And a brace, a `//` or a `target:` inside a string literal is
none of those, so strings are read in the same scan as the comments — either
one taken first swallows the other's delimiter — and every key is looked for
in the view with the literals blanked.

The workflow half reads YAML through `scripts/lint_workflows.py`, the
repository's one workflow reader, rather than matching the raw text: a step is
a mapping there whether it leads with `uses:` or with `name:`, and a value is
the value whatever quoting or trailing comment it was written with.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
from typing import Any

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NPMRC = os.path.join(_REPO, "web", ".npmrc")
_PACKAGE_JSON = os.path.join(_REPO, "web", "package.json")
_LOCKFILE = os.path.join(_REPO, "web", "package-lock.json")
_NODE_VERSION = os.path.join(_REPO, ".node-version")
_MISE = os.path.join(_REPO, "mise.toml")
_VITE_CONFIG = os.path.join(_REPO, "web", "vite.config.ts")


def _load_lint_workflows():
    """Import scripts/lint_workflows.py by path; `scripts/` is not a package."""
    path = os.path.join(_REPO, "scripts", "lint_workflows.py")
    spec = importlib.util.spec_from_file_location("lint_workflows", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lint_workflows"] = module
    spec.loader.exec_module(module)
    return module


wf = _load_lint_workflows()

_PIN_FILE = ".node-version"
_VERSION_SPEC = re.compile(r"\A\d+(\.\d+){0,2}\Z")
_SETUP_NODE = "actions/setup-node@"
_STATED_VERSION = "node-version"

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

# The locked packages whose install script has been read and found skippable.
# fsevents ships its prebuilt binding in the tarball, so the `node-gyp rebuild`
# its `install` script runs is a fallback that never has to fire.
_SKIPPABLE_INSTALL_SCRIPTS = frozenset({"node_modules/fsevents"})

_BUILD_BLOCK = re.compile(r"\bbuild\s*:\s*\{")
# The key alone, matched against the string-blanked view so a `target:` that
# lives inside a string literal is not one of build's keys.
_TARGET_KEY = re.compile(r"\btarget\s*:")
# Either shape Vite accepts: a list of targets, or one target on its own.
_TARGET = re.compile(r"""\btarget\s*:\s*(?P<value>\[[^]]*]|["'`][^"'`]*["'`])""")
_QUOTED = re.compile(r"""["'`]([^"'`]*)["'`]""")
# A browser and the version it is supported from, as esbuild and Lightning CSS
# name it: `chrome111`, `safari16.4`, `ios16.4`.
_BROWSER_VERSION = re.compile(r"\A[a-z]+\d+(?:\.\d+)*\Z")


def _text_and_code(source: str) -> tuple[str, str]:
    """`source` with comments dropped, and that same text with strings blanked.

    Both come out of one scan because neither can be found without the other:
    an apostrophe in `// the console's floor` opens a string literal for a
    reader that takes strings first, and the `//` inside
    `"http://127.0.0.1:8123"` opens a comment for one that takes comments
    first — each swallowing code up to the next delimiter, closing braces
    included.

    The two are equal in length, so an index into the blanked text indexes the
    kept text: a brace or a `target:` found in the first is that character in
    the second, and one that only appears inside a string is not found at all.
    """
    text: list[str] = []
    code: list[str] = []
    index = 0
    quote = ""
    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if quote:
            text.append(char)
            code.append(" ")
            if char == "\\" and index + 1 < len(source):
                text.append(source[index + 1])
                code.append(" ")
                index += 2
                continue
            if char == quote:
                quote = ""
        elif pair == "//":
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            continue
        elif pair == "/*":
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        elif char in "\"'`":
            quote = char
            text.append(char)
            code.append(" ")
        else:
            text.append(char)
            code.append(char)
        index += 1
    return "".join(text), "".join(code)


def _build_options(source: str) -> tuple[str, str]:
    """A Vite config's `build: { … }` options, its nested objects dropped.

    Both views `_text_and_code` yields, cut to the same span and so still
    equal in length: the options as written, and the same text with its string
    literals blanked. A key is looked for in the second and read out of the
    first, because a `target:` written inside a string is not a key of
    `build` — and a config whose only `target:` sits in a `banner` string
    would otherwise read as stating a floor with `build.target` deleted.

    Scoped to that block because every `server.proxy` entry states a `target`
    of its own: a reader that took the first one in the file would read a proxy
    URL as the browser floor and pass for a config that states no floor. And
    scoped to the block's own keys, because a `target` nested under it — an
    option of `rollupOptions`, say — is not the floor either, and a reader that
    took the first one *inside* the block would pass for the same config with
    `build.target` deleted from it.

    A nested object's key is kept, only its body goes: what is dropped is
    everything a `{` opens.
    """
    text, code = _text_and_code(source)
    opened = _BUILD_BLOCK.search(code)
    if opened is None:
        return "", ""
    depth = 0
    own_text: list[str] = []
    own_code: list[str] = []
    for index in range(opened.end() - 1, len(code)):
        char = code[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return "".join(own_text), "".join(own_code)
        elif depth == 1:
            own_text.append(text[index])
            own_code.append(char)
    return "", ""


def _declared_target(source: str) -> list[str] | None:
    """The `build.target` a Vite config states, or None if it states none."""
    text, code = _build_options(source)
    key = _TARGET_KEY.search(code)
    if key is None:
        return None
    stated = _TARGET.match(text, key.start())
    return None if stated is None else _QUOTED.findall(stated.group("value"))


def _vite_config() -> str:
    with open(_VITE_CONFIG, encoding="utf-8") as handle:
        return handle.read()


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


def _install_scripted() -> set[str]:
    """Every locked package npm would run an install script for."""
    with open(_LOCKFILE, encoding="utf-8") as handle:
        packages = json.load(handle)["packages"]
    return {name for name, entry in packages.items() if entry.get("hasInstallScript")}


def _workflows(directory: str | None = None) -> list[tuple[str, Any]]:
    """Every workflow, as (file name, the parsed document)."""
    paths = wf.workflow_paths(directory)
    assert paths, "no workflow files found — this module is reading the wrong directory"
    return [(os.path.basename(path), wf.load(path)) for path in paths]


def _steps(directory: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    """Every step in every workflow, as (file name, job id, the step mapping)."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for name, document in _workflows(directory):
        for job_id, job in wf.jobs(document).items():
            declared = job.get("steps") if isinstance(job, dict) else None
            for step in declared if isinstance(declared, list) else []:
                if isinstance(step, dict):
                    found.append((name, job_id, step))
    return found


def _setup_node_steps(directory: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    """The steps that run `actions/setup-node`."""
    return [
        (name, job_id, step)
        for name, job_id, step in _steps(directory)
        if str(step.get("uses", "")).startswith(_SETUP_NODE)
    ]


def _stated_versions(document: Any) -> list[Any]:
    """Every `node-version:` value anywhere in one workflow.

    A step's `with:` is not the only place a version can be written. A
    `strategy.matrix` entry states one, and a job that calls a reusable
    workflow states one in a job-level `with:` — that job declares no
    `steps:` at all, so a reader that walks steps never reaches it.
    """
    if isinstance(document, dict):
        stated = [value for key, value in document.items() if key == _STATED_VERSION]
        return stated + [found for value in document.values() for found in _stated_versions(value)]
    if isinstance(document, list):
        return [found for item in document for found in _stated_versions(item)]
    return []


def _inputs(step: dict[str, Any]) -> dict[str, Any]:
    declared = step.get("with")
    return declared if isinstance(declared, dict) else {}


class InstallScriptsTest(unittest.TestCase):
    def test_install_lifecycle_scripts_are_off(self):
        self.assertIn(
            _npmrc().get("ignore-scripts", "").lower(),
            _TRUE,
            "without ignore-scripts, every dependency's preinstall/install/postinstall "
            "runs on `npm ci` in CI and on a contributor's `npm install`",
        )


class DependencyInstallScriptTest(unittest.TestCase):
    """The dependency half of `ignore-scripts`: which scripts it drops."""

    def test_every_install_script_it_skips_has_been_read(self):
        self.assertEqual(
            set(_SKIPPABLE_INSTALL_SCRIPTS),
            _install_scripted(),
            "web/.npmrc skips these — a new one installs a package that may need "
            "its script to place a binary, and an entry with no package left to "
            "name is an allowance for nothing",
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
        for name, document in _workflows():
            with self.subTest(workflow=name):
                self.assertEqual(
                    [],
                    _stated_versions(document),
                    f"a version written here can disagree with {_PIN_FILE}; "
                    "use `node-version-file` instead",
                )

    def test_every_setup_node_reads_the_pin_file(self):
        steps = _setup_node_steps()
        assert steps, "no setup-node step found — a guard that checks nothing is not a pass"
        for name, job_id, step in steps:
            with self.subTest(workflow=name, job=job_id):
                self.assertEqual(
                    _PIN_FILE,
                    _inputs(step).get("node-version-file"),
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


class BrowserFloorTest(unittest.TestCase):
    def test_the_floor_is_stated(self):
        self.assertTrue(
            _declared_target(_vite_config()),
            "web/vite.config.ts states no build.target, so the console's browser "
            "floor is Vite's default again and the next Vite bump moves it — as a "
            "rebuilt bundle indistinguishable from a re-minified one",
        )

    def test_every_browser_in_the_floor_carries_a_version(self):
        stated = _declared_target(_vite_config())
        assert stated, "no build.target stated; a loop over nothing is not a pass"
        for entry in stated:
            with self.subTest(target=entry):
                self.assertRegex(
                    entry,
                    _BROWSER_VERSION,
                    "a target such as `baseline-widely-available` is Vite's moving "
                    "default written out rather than a floor: it names a different "
                    "set of browsers per Vite version",
                )


_VITE_SHAPES = """\
export default defineConfig({
  build: {
    outDir: "../c64cast/web/dist",
    // target: ["chrome1"],
    target: ["chrome111", "safari16.4"],
    rollupOptions: { output: { assetFileNames: "assets/app.[ext]" } },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8123", ws: true },
    },
  },
});
"""

_VITE_NO_FLOOR = """\
export default defineConfig({
  build: { outDir: "../c64cast/web/dist" },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8123" } } },
});
"""

_VITE_ONE_TARGET = 'export default defineConfig({ build: { target: "chrome111" } });\n'

_VITE_PROXY_FIRST = """\
export default defineConfig({
  server: { proxy: { "/api": { target: "http://127.0.0.1:8123" } } },
  build: { target: ["chrome111"] },
});
"""

# `build.target` deleted, with an option nested under `build` that has a
# `target` of its own left standing in front of where it was.
_VITE_NESTED_TARGET = """\
export default defineConfig({
  build: {
    rollupOptions: { output: { target: "chrome111" } },
  },
});
"""

# An apostrophe in a comment, and a brace inside a string — each of which ends
# the `build` block early for a reader that takes strings and comments apart.
_VITE_AWKWARD_LITERALS = """\
export default defineConfig({
  build: {
    // The console's browser floor.
    target: ["chrome111"],
    rollupOptions: { output: { banner: "} //" } },
  },
});
"""

# `build.target` deleted, with a `target:` left inside a string among build's
# own keys — text a rollup `banner` or `footer` can carry verbatim.
_VITE_STRING_TARGET = """\
export default defineConfig({
  build: {
    footer: "target: 'chrome111'",
  },
});
"""


class ViteConfigReadingTest(unittest.TestCase):
    """The config reader against the shapes a Vite config can be written in."""

    def test_a_commented_out_target_is_not_read_as_the_stated_one(self):
        self.assertEqual(["chrome111", "safari16.4"], _declared_target(_VITE_SHAPES))

    def test_one_target_on_its_own_reads_as_one_entry(self):
        self.assertEqual(["chrome111"], _declared_target(_VITE_ONE_TARGET))

    def test_a_config_stating_no_floor_reads_as_stating_none(self):
        self.assertIsNone(_declared_target(_VITE_NO_FLOOR))

    def test_a_nested_object_does_not_end_the_build_block(self):
        self.assertIn("rollupOptions", _build_options(_VITE_SHAPES)[0])

    def test_a_target_nested_under_build_is_not_read_as_the_floor(self):
        self.assertIsNone(_declared_target(_VITE_NESTED_TARGET))

    def test_a_target_inside_a_string_is_not_read_as_the_floor(self):
        self.assertIsNone(_declared_target(_VITE_STRING_TARGET))

    def test_a_brace_or_comment_start_inside_a_string_is_neither(self):
        self.assertEqual(["chrome111"], _declared_target(_VITE_AWKWARD_LITERALS))

    def test_a_proxy_url_ahead_of_the_build_block_is_not_a_comment(self):
        self.assertEqual(["chrome111"], _declared_target(_VITE_PROXY_FIRST))


_SHAPES = """\
name: shapes
jobs:
  named:
    steps:
      - name: Set up Node
        uses: actions/setup-node@abc
        with:
          node-version-file: '.node-version'   # quoted, with a comment
      - run: node --test x.mjs
  terse:
    steps:
      - uses: actions/setup-node@abc
  other:
    steps:
      - uses: actions/checkout@abc
  called:
    uses: ./.github/workflows/build.yml
    with:
      node-version: '22'
"""


class WorkflowReadingTest(unittest.TestCase):
    """The reading helpers against the shapes a workflow can be written in."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        with open(os.path.join(self.directory, "shapes.yml"), "w", encoding="utf-8") as handle:
            handle.write(_SHAPES)
        self.found = _setup_node_steps(self.directory)
        self.by_job = {job_id: step for _, job_id, step in self.found}
        ((_, self.document),) = _workflows(self.directory)

    def test_both_step_shapes_are_seen_and_nothing_else_is(self):
        self.assertEqual(["named", "terse"], [job_id for _, job_id, _ in self.found])

    def test_a_quoted_commented_value_is_the_file_it_names(self):
        self.assertEqual(_PIN_FILE, _inputs(self.by_job["named"]).get("node-version-file"))

    def test_a_step_naming_no_version_file_reads_as_naming_none(self):
        self.assertIsNone(_inputs(self.by_job["terse"]).get("node-version-file"))

    def test_a_version_stated_outside_any_step_is_still_seen(self):
        self.assertIn("22", _stated_versions(self.document))

    def test_a_version_file_is_not_read_as_a_stated_version(self):
        self.assertEqual(["22"], _stated_versions(self.document))
