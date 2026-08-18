"""Tests for secret redaction on the durable and shared log destinations.

The split under test is deliberate and easy to regress in either direction: the
web console's token has to stay *intact* on the terminal, because the login URL
printed there is the only entry point a phone gets, and has to be *gone* from
`--log-file`, which outlives the run and is not created `0600`. The buffer half
of the same split is in `test_serve.py`, next to the buffer.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest

from c64cast._redact import redact_secrets
from c64cast.app import cli_commands


class RedactSecretsTest(unittest.TestCase):
    def test_a_token_value_is_replaced(self):
        self.assertEqual(
            redact_secrets("open http://host:8123/api/login?token=abc123&next=/"),
            "open http://host:8123/api/login?token=REDACTED&next=/",
        )

    def test_the_rest_of_the_line_survives(self):
        """Redaction has to leave a diagnostic line behind — a log that cannot
        say which address was printed is not worth keeping."""
        out = redact_secrets("web console: open http://127.0.0.1:8123/api/login?token=xyz&next=/")
        self.assertIn("127.0.0.1:8123", out)
        self.assertIn("next=/", out)
        self.assertNotIn("xyz", out)

    def test_any_token_parameter_is_covered(self):
        """Keyed on the `token=` suffix, so a viewer token — or one added
        later — is redacted without naming it here."""
        out = redact_secrets("viewer_token=vvv and token=aaa")
        self.assertNotIn("vvv", out)
        self.assertNotIn("aaa", out)
        self.assertEqual(out.count("REDACTED"), 2)

    def test_a_line_with_no_secret_is_untouched(self):
        line = "web console: editable config roots: shows = /home/kfox/shows"
        self.assertEqual(redact_secrets(line), line)

    def test_a_quoted_token_stops_at_the_quote(self):
        self.assertEqual(redact_secrets('{"token=abc"}'), '{"token=REDACTED"}')


class LogFileRedactionTest(unittest.TestCase):
    """`configure_logging` reconfigures the root logger, so each test undoes it."""

    def setUp(self):
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        self.addCleanup(setattr, root, "level", level)

        def restore() -> None:
            for h in list(root.handlers):
                root.removeHandler(h)
                h.close()
            for h in handlers:
                root.addHandler(h)

        self.addCleanup(restore)

    def _log_to_file(self, message: str) -> str:
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        cli_commands.configure_logging(0, log_file=path)
        logging.getLogger("c64cast").info("%s", message)
        for h in logging.getLogger().handlers:
            h.flush()
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_log_file_never_holds_a_token(self):
        written = self._log_to_file(
            "web console: open http://127.0.0.1:8123/api/login?token=s3cr3t&next=/"
        )
        self.assertNotIn("s3cr3t", written)
        self.assertIn("token=REDACTED", written)

    def test_an_ordinary_line_reaches_the_file_unchanged(self):
        self.assertIn("scene 2 of 4: starting", self._log_to_file("scene 2 of 4: starting"))

    def test_the_terminal_handler_is_not_redacting(self):
        """The operator's own screen is the one place the token has to work:
        that URL is how a phone gets in."""
        cli_commands.configure_logging(0, log_file=None)
        terminal = logging.getLogger().handlers[0]
        self.assertNotIsInstance(terminal.formatter, cli_commands.RedactingFormatter)


if __name__ == "__main__":
    unittest.main()
