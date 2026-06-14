"""Fuzzy 'did you mean' suggestions on typo'd config keys (#5b).

Two surfaces: unknown section keys (warn-logged at load) and unknown overlay
constructor kwargs (raised at build). Both should point at the intended key.
"""

from __future__ import annotations

import logging
import unittest

from c64cast import config as cfgmod
from c64cast import overlays as ovmod


class SectionKeySuggestionTest(unittest.TestCase):
    def test_warns_with_suggestion(self):
        dc = cfgmod.AudioCfg()
        with self.assertLogs("c64cast.config", level="WARNING") as cm:
            cfgmod._apply_section(dc, {"enabledd": True}, "audio")
        joined = "\n".join(cm.output)
        self.assertIn("enabledd", joined)
        self.assertIn("did you mean", joined)
        self.assertIn("enabled", joined)
        # The typo'd key must NOT have been applied.
        self.assertFalse(dc.enabled)

    def test_no_suggestion_when_nothing_close(self):
        dc = cfgmod.AudioCfg()
        with self.assertLogs("c64cast.config", level="WARNING") as cm:
            cfgmod._apply_section(dc, {"zzzzzz": 1}, "audio")
        joined = "\n".join(cm.output)
        self.assertIn("zzzzzz", joined)
        self.assertNotIn("did you mean", joined)


class OverlayKwargSuggestionTest(unittest.TestCase):
    def test_bad_kwarg_suggests_real_param(self):
        with self.assertRaises(ValueError) as ctx:
            ovmod.build_overlay({"type": "clock", "colour": "red"}, None)
        msg = str(ctx.exception)
        self.assertIn("did you mean", msg)
        self.assertIn("fg_color", msg)

    def test_unknown_overlay_type_lists_known(self):
        with self.assertRaises(ValueError) as ctx:
            ovmod.build_overlay({"type": "clocck"}, None)
        self.assertIn("unknown overlay type", str(ctx.exception))


if __name__ == "__main__":
    # Keep the suggestion-warning visible when run directly.
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
