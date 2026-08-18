"""Tests for secret redaction on the durable and shared log destinations.

The split under test is deliberate and easy to regress in either direction: the
web console's token has to stay *intact* on the terminal, because the login URL
printed there is the only entry point a phone gets, and has to be *gone* from
`--log-file`, which outlives the run and is not created `0600`. The buffer half
of the same split is in `test_serve.py`, next to the buffer.

Nothing here reconfigures the root logger. `configure_logging` clears the root
handlers and installs its own, and that outlives the test — the hazard
`_fakes.quiet_logging` exists for. So the end-to-end check drives a handler the
test owns outright, and the wiring check inspects what `configure_logging`
attached without emitting through it.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest

from c64cast._redact import redact_secrets
from c64cast.app import cli_commands

LOGIN_LINE = "web console: open http://127.0.0.1:8123/api/login?token=s3cr3t&next=/"


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("c64cast", logging.INFO, __file__, 1, message, None, None)


class RedactSecretsTest(unittest.TestCase):
    def test_a_token_value_is_replaced(self):
        self.assertEqual(
            redact_secrets("open http://host:8123/api/login?token=abc123&next=/"),
            "open http://host:8123/api/login?token=REDACTED&next=/",
        )

    def test_the_rest_of_the_line_survives(self):
        """Redaction has to leave a diagnostic line behind — a log that cannot
        say which address was printed is not worth keeping."""
        out = redact_secrets(LOGIN_LINE)
        self.assertIn("127.0.0.1:8123", out)
        self.assertIn("next=/", out)
        self.assertNotIn("s3cr3t", out)

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


class RedactingFormatterTest(unittest.TestCase):
    def test_it_redacts_what_it_formats(self):
        formatted = cli_commands.RedactingFormatter("%(message)s").format(_record(LOGIN_LINE))
        self.assertNotIn("s3cr3t", formatted)
        self.assertIn("token=REDACTED", formatted)

    def test_an_ordinary_line_is_unchanged(self):
        formatted = cli_commands.RedactingFormatter("%(message)s").format(_record("scene 2 of 4"))
        self.assertEqual(formatted, "scene 2 of 4")

    def test_a_file_handler_wearing_it_writes_no_token(self):
        """The end-to-end the formatter exists for, on a handler this test owns
        rather than on the root logger."""
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        handler = logging.FileHandler(path, encoding="utf-8")
        self.addCleanup(handler.close)
        handler.setFormatter(cli_commands.RedactingFormatter("%(message)s"))
        handler.handle(_record(LOGIN_LINE))
        handler.flush()
        with open(path, encoding="utf-8") as fh:
            written = fh.read()
        self.assertNotIn("s3cr3t", written)
        self.assertIn("token=REDACTED", written)


class ConfigureLoggingWiringTest(unittest.TestCase):
    """`configure_logging` reconfigures the root logger, so each test undoes it
    — restoring the handler list rather than closing anything, since the
    handlers it replaced belong to whoever installed them."""

    def setUp(self):
        root = logging.getLogger()
        self.handlers, self.level = root.handlers[:], root.level

        def restore() -> None:
            for h in root.handlers[:]:
                if h not in self.handlers:
                    h.close()
            root.handlers[:] = self.handlers
            root.setLevel(self.level)

        self.addCleanup(restore)

    def test_the_log_file_handler_is_redacting(self):
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        cli_commands.configure_logging(0, log_file=path)
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(files), 1)
        self.assertIsInstance(files[0].formatter, cli_commands.RedactingFormatter)

    def test_the_terminal_handler_is_not(self):
        """The operator's own screen is the one place the token has to work:
        that URL is how a phone gets in."""
        cli_commands.configure_logging(0, log_file=None)
        for handler in logging.getLogger().handlers:
            self.assertNotIsInstance(handler.formatter, cli_commands.RedactingFormatter)


if __name__ == "__main__":
    unittest.main()
