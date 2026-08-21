"""Contract tests for cli.build_parser — the argparse layer itself.

The flag→config *mapping* is covered in test_quickcast (asserted against
CLI_TO_CFG so a newly mapped flag is covered the day it's added); these
pin the parser-level invariants that everything downstream assumes.

Plus the four commands whose whole job is answering "which install is this,
and is it current?" — `--version`, `--print-schema-path`,
`--check-for-updates` and `--upgrade`. All four exist because an upgrade is
easy to believe you have done and hard to see, so what they print (or run) is
the contract; the mechanism itself (install detection, the PyPI query,
running the installer) is tested in test_upgrade.py, not here.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import quiet_logging

import c64cast
from c64cast import __version__
from c64cast.app import config_serialize as ser
from c64cast.app import paths, upgrade
from c64cast.app.cli import _version_text, build_parser, main

# The documented flag groups (CLAUDE.md "Flag groups (-h shows them
# grouped)"). argparse's default groups are excluded below.
DOCUMENTED_GROUPS = {
    "connection",
    "quick playback (with MEDIA args)",
    "video input",
    "audio",
    "vision input",
    "playlist",
    "web console",
    "introspection",
    "updates",
    "debug",
}


def _run(argv: list[str]) -> tuple[int, str]:
    # quiet_logging() undoes any root-logger reconfiguration a config-free
    # command's configure_logging() call performs — without it, that
    # handler outlives this test and every later INFO record in the same
    # worker process prints to the console (see _fakes.quiet_logging).
    buf = io.StringIO()
    with quiet_logging(), contextlib.redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


class ParserContractTest(unittest.TestCase):
    def test_every_config_bearing_flag_defaults_to_none(self):
        # The merge_cli contract: default=None is how the cascade tells
        # "not provided" from "explicitly set to the default value". A flag
        # added with a real argparse default would silently override the
        # TOML on every run. Only the config-free command switches
        # (--doctor, --list-* and friends, all store_true) and the MEDIA
        # positional are allowed a non-None default.
        ns = build_parser().parse_args([])
        self.assertEqual(ns.inputs, [], "positional MEDIA defaults to an empty list")
        for dest, value in vars(ns).items():
            if dest == "inputs" or value is False:
                continue
            self.assertIsNone(value, f"--{dest} must default to None for merge_cli")

    def test_every_mapped_flag_defaults_to_none(self):
        # The flags CLI_TO_CFG maps into config fields are exactly the ones
        # the None contract exists for — pin them individually so a False
        # default can't hide behind the store_true exemption above.
        from c64cast.app.config import CLI_TO_CFG

        ns = build_parser().parse_args([])
        for dest in CLI_TO_CFG:
            self.assertIsNone(getattr(ns, dest), f"--{dest} must default to None")

    def test_no_dma_password_flag_exists(self):
        # The DMA password is env/TOML only — a CLI flag would leak the
        # secret into shell history and `ps` output.
        parser = build_parser()
        for action in parser._actions:
            for opt in action.option_strings:
                self.assertNotIn("password", opt.lower(), f"secret-bearing flag {opt}")
        self.assertNotIn("dma_password", vars(parser.parse_args([])))

    def test_documented_flag_groups_exist(self):
        titles = {g.title for g in build_parser()._action_groups}
        missing = DOCUMENTED_GROUPS - titles
        self.assertFalse(missing, f"documented flag groups missing from the parser: {missing}")

    def test_verbosity_counts(self):
        self.assertEqual(build_parser().parse_args(["-v"]).verbose, 1)
        self.assertEqual(build_parser().parse_args(["-vv"]).verbose, 2)

    def test_loop_is_a_boolean_pair_on_one_dest(self):
        self.assertIs(build_parser().parse_args(["--loop"]).loop, True)
        self.assertIs(build_parser().parse_args(["--no-loop"]).loop, False)

    def test_connection_target_and_media_parse_together(self):
        ns = build_parser().parse_args(["-u", "tr://", "clip.mp4", "tune.sid"])
        self.assertEqual(ns.url, "tr://")
        self.assertEqual(ns.inputs, ["clip.mp4", "tune.sid"])

    def test_version_names_the_install_it_runs_from(self):
        # "I upgraded and it still reports the old version" is answered by the
        # path, not the number: it names the environment the PATH command
        # actually points into. argparse prints --version to stdout and exits.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args(["--version"])
        printed = out.getvalue().strip()
        self.assertTrue(printed.startswith(f"c64cast {__version__} ("), printed)
        self.assertIn(str(Path(c64cast.__file__).resolve().parent.parent), printed)

    def test_version_text_carries_no_percent_for_argparse_to_expand(self):
        # argparse %-formats the version string only when it contains
        # "%(prog)", which is why this one spells the program name out: an
        # install path with a literal % in it would otherwise raise here.
        self.assertNotIn("%(prog)", _version_text())

    def test_print_schema_path_names_this_installs_schema(self):
        # The line an editor is pointed at has to name the schema *this* build
        # generates — that is what stops it from going stale on the next
        # upgrade, which rewrites exactly that file.
        rc, out = _run(["--print-schema-path"])
        self.assertEqual(rc, 0)
        resolved = Path(os.path.abspath(out.strip()))
        self.assertEqual(resolved, paths.packaged_schema_path())

    def test_print_schema_path_answers_for_the_config_it_is_given(self):
        # Relative-vs-absolute depends on where the config sits (see
        # config_serialize.schema_directive_for), so the command has to honor
        # --config rather than assume ./c64cast.toml.
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "show.toml")
            rc, out = _run(["--config", cfg, "--print-schema-path"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), ser.schema_directive_for(cfg))

    def test_print_schema_path_prints_a_value_not_a_directive(self):
        # Just the value, so `#:schema ` in front of it is a config's first line
        # and the bare output is what an editor's schema association wants.
        self.assertNotIn("#:schema", _run(["--print-schema-path"])[1])

    def test_check_for_updates_dispatches_before_config_load(self):
        # Mocked at the upgrade.py boundary (not requests/subprocess) so this
        # stays a dispatch-order test, not a re-test of the PyPI query itself
        # (that's test_upgrade.py's job) — and never touches the network.
        with mock.patch.object(upgrade, "latest_release", return_value="0.3.0"):
            rc, out = _run(["--check-for-updates"])
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)

    def test_upgrade_dispatches_before_config_load(self):
        # "unknown" install prints its explanation to stderr, not stdout —
        # capture both so nothing leaks into the test run's own output.
        err = io.StringIO()
        with (
            mock.patch.object(upgrade, "detect_install") as detect,
            contextlib.redirect_stderr(err),
        ):
            detect.return_value = upgrade.Install("unknown", Path("/x"), None)
            rc, _out = _run(["--upgrade", "--yes"])
        self.assertEqual(rc, 2)  # "unknown" install: nothing to run, exit 2
        detect.assert_called_once()

    def test_check_for_updates_and_upgrade_and_yes_default_to_false(self):
        ns = build_parser().parse_args([])
        self.assertIs(ns.check_for_updates, False)
        self.assertIs(ns.upgrade, False)
        self.assertIs(ns.yes, False)

    def test_check_for_updates_and_upgrade_and_yes_parse(self):
        ns = build_parser().parse_args(["--check-for-updates", "--upgrade", "--yes"])
        self.assertIs(ns.check_for_updates, True)
        self.assertIs(ns.upgrade, True)
        self.assertIs(ns.yes, True)

    def test_system_choices_are_the_two_video_standards(self):
        self.assertEqual(build_parser().parse_args(["-s", "PAL"]).system, "PAL")
        # argparse prints usage + the rejection to stderr before it exits.
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            build_parser().parse_args(["-s", "SECAM"])
        self.assertIn("invalid choice: 'SECAM'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
