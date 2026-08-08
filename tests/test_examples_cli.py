"""Tests for the packaged-example CLI surface: `--list-examples`,
`--print-example NAME`, and `--config example:NAME`.

Drives the real argparse entry point. The point of the whole feature is that a
demo is reachable *by name* with no checkout, so these assert the three ways a
user touches it: list them, copy one out, run one. `--config example:NAME` is
checked through `_resolve_configs` (the single resolution hook) rather than a
full run, since running one needs hardware.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from _fakes import MachineSettingsIsolation

from c64cast.app import config as cfgmod
from c64cast.app import introspect, paths
from c64cast.app.cli import _resolve_configs, build_parser, main

# Loading a demo applies the machine-settings layer; isolate it so a real
# ~/.config/c64cast/settings.toml on the dev's machine can't change what the
# resolved configs look like.
_settings_isolation = MachineSettingsIsolation()


def setUpModule():
    _settings_isolation.start()


def tearDownModule():
    _settings_isolation.stop()


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


class ListExamplesTest(unittest.TestCase):
    def test_lists_every_packaged_demo_by_name(self):
        rc, out = _run(["--list-examples"])
        self.assertEqual(rc, 0)
        for path in paths.example_config_paths():
            self.assertIn(paths.example_name(path), out)

    def test_shows_how_to_run_and_copy_one(self):
        _, out = _run(["--list-examples"])
        self.assertIn("--config example:", out)
        self.assertIn("--print-example", out)

    def test_summaries_come_from_the_files_own_header(self):
        summary = introspect.example_summary(paths.resolve_example("hello"))
        self.assertIn("hello world", summary)
        # The schema directive is not prose, and neither is the boilerplate
        # prefix nearly every demo repeats.
        self.assertNotIn("#:schema", summary)
        self.assertNotIn("Single-scene demo", summary)
        # ...and it reaches the listing (which wraps, so match the opening).
        self.assertIn(summary.split(" — ")[0], _run(["--list-examples"])[1])

    def test_demos_needing_user_media_are_tagged(self):
        _, out = _run(["--list-examples"])
        self.assertIn("needs your own media", out)
        # A scene sourcing from the empty `assets/` tree needs a file dropped
        # in; an overlay's missing file (logo) draws a placeholder instead, so
        # tagging it would send users looking for a problem they don't have.
        self.assertTrue(introspect.example_needs_media(paths.resolve_example("scene-slideshow")))
        self.assertFalse(introspect.example_needs_media(paths.resolve_example("overlay-logo")))
        self.assertFalse(introspect.example_needs_media(paths.resolve_example("hello")))


class PrintExampleTest(unittest.TestCase):
    def test_prints_the_file_verbatim(self):
        rc, out = _run(["--print-example", "hello"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, paths.resolve_example("hello").read_text(encoding="utf-8"))

    def test_output_is_a_loadable_config(self):
        # The documented way to make a demo yours is to redirect this into a
        # file, so what comes out has to survive the loader from there.
        _, out = _run(["--print-example", "hello"])
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "c64cast.toml")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(out)
            cfg = cfgmod.load(dest)
        self.assertEqual(len(cfg.scenes), 1)

    def test_unknown_name_is_a_usage_error(self):
        self.assertEqual(main(["--print-example", "nope"]), 2)


class ConfigExamplePrefixTest(unittest.TestCase):
    def _resolve(self, spec: str):
        args = build_parser().parse_args(["--config", spec])
        return _resolve_configs(args), args

    def test_runs_a_demo_by_name(self):
        (loaded, cfgs), args = self._resolve("example:hello")
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(len(cfgs[0].scenes), 1)
        # The prefix is gone by the time anything downstream reads the path.
        self.assertEqual(args.config, str(paths.resolve_example("hello")))
        self.assertEqual(loaded.paths[0], str(paths.resolve_example("hello")))

    def test_ensemble_demo_resolves_its_per_system_files(self):
        # The master names left/middle/right relative to its own directory —
        # the reason the resolver must produce a real filesystem path.
        (loaded, cfgs), _ = self._resolve("example:ensemble/master")
        self.assertTrue(loaded.is_ensemble)
        self.assertEqual(loaded.names, ["left", "middle", "right"])
        self.assertEqual(len(cfgs), 3)

    def test_unknown_name_is_a_usage_error(self):
        args = build_parser().parse_args(["--config", "example:nope"])
        with self.assertRaises(ValueError):
            _resolve_configs(args)

    def test_a_plain_path_is_untouched(self):
        args = build_parser().parse_args(["--config", "does-not-exist.toml"])
        with self.assertRaises(cfgmod.ConfigError):
            _resolve_configs(args)
        self.assertEqual(args.config, "does-not-exist.toml")


if __name__ == "__main__":
    unittest.main()
