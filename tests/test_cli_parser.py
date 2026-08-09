"""Contract tests for cli.build_parser — the argparse layer itself.

The flag→config *mapping* is covered in test_quickcast (asserted against
CLI_TO_CFG so a newly mapped flag is covered the day it's added); these
pin the parser-level invariants that everything downstream assumes.
"""

from __future__ import annotations

import unittest

from c64cast.app.cli import build_parser

# The documented flag groups (CLAUDE.md "Flag groups (-h shows them
# grouped)"). argparse's default groups are excluded below.
DOCUMENTED_GROUPS = {
    "connection",
    "quick playback (with MEDIA args)",
    "video input",
    "audio",
    "vision input",
    "playlist",
    "introspection",
    "debug",
}


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

    def test_system_choices_are_the_two_video_standards(self):
        self.assertEqual(build_parser().parse_args(["-s", "PAL"]).system, "PAL")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["-s", "SECAM"])


if __name__ == "__main__":
    unittest.main()
