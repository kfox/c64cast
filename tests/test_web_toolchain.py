"""Guards for the npm toolchain configuration under `web/`.

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
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NPMRC = os.path.join(_REPO, "web", ".npmrc")
_PACKAGE_JSON = os.path.join(_REPO, "web", "package.json")

# The hooks npm fires on its own, with no `npm run` naming them.
_SELF_FIRING_HOOKS = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "prepare",
        "prepack",
        "postpack",
        "prepublish",
        "prepublishOnly",
        "dependencies",
    }
)

_TRUE = frozenset({"true", "1", "yes", "on"})


def _npmrc() -> dict[str, str]:
    """`web/.npmrc` as a mapping, ignoring blank lines and comments."""
    settings: dict[str, str] = {}
    with open(_NPMRC, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line[0] in ";#":
                continue
            key, sep, value = line.partition("=")
            assert sep, f"{line!r} in web/.npmrc is not a `key=value` line"
            settings[key.strip()] = value.strip()
    return settings


def _scripts() -> dict[str, str]:
    with open(_PACKAGE_JSON, encoding="utf-8") as handle:
        return dict(json.load(handle).get("scripts", {}))


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
