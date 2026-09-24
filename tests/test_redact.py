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

from _fakes import RestoresLogging

from c64cast._redact import redact_secrets, redact_source_line
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

    def test_a_json_rendering_with_a_colon_and_spaces_is_covered(self):
        out = redact_secrets('{"token": "s3cr3t", "next": "/"}')
        self.assertNotIn("s3cr3t", out)
        self.assertIn('"token": "REDACTED"', out)
        self.assertIn('"next": "/"', out)

    def test_a_toml_rendering_with_spaces_around_equals_is_covered(self):
        out = redact_secrets('token = "s3cr3t"')
        self.assertNotIn("s3cr3t", out)

    def test_a_single_quoted_rendering_is_covered(self):
        """A TOML literal string and a Python mapping repr both quote with `'`,
        and `config._format_toml_error` quotes the offending config line."""
        self.assertNotIn("hunter2", redact_secrets("dma_password = 'hunter2'"))
        out = redact_secrets("{'token': 's3cr3t', 'next': '/'}")
        self.assertNotIn("s3cr3t", out)
        self.assertIn("'next': '/'", out)

    def test_a_quoted_value_runs_to_its_matching_quote(self):
        """A quoted value is a whole secret: a passphrase DMA password holds
        spaces, and a generated key holds `&` and `,`."""
        for line in (
            'dma_password = "correct horse battery staple" oops',
            "dma_password = 'correct horse battery staple' oops",
            "dma_password = '''correct horse battery staple''' oops",
            'dma_password = """correct horse battery staple""" oops',
        ):
            with self.subTest(line=line):
                out = redact_secrets(line)
                for word in ("correct", "horse", "battery", "staple"):
                    self.assertNotIn(word, out)
                self.assertIn("oops", out)
        self.assertEqual(redact_secrets("api_key = 'AAAA&BBBB'"), "api_key = 'REDACTED'")
        self.assertEqual(redact_secrets("password = 'p@ss,word'"), "password = 'REDACTED'")

    def test_an_unterminated_quote_takes_the_rest_of_the_line(self):
        """The unterminated string is what `_format_toml_error` is quoting in
        the first place — the parse failed on that line."""
        out = redact_secrets('dma_password = "correct horse battery staple')
        self.assertNotIn("staple", out)

    def test_neither_bound_crosses_a_newline(self):
        """One formatted record can hold several lines, so the mask stops where
        the line does: a widened bound would blank whatever followed."""
        out = redact_secrets('dma_password = "first\nsecond"')
        self.assertNotIn("first", out)
        self.assertIn("second", out)

    def test_a_delimiter_that_ends_the_line_is_left_alone(self):
        """No part of the value is on the key's line, and a mask there would
        read as coverage the line-bounded value does not have."""
        line = 'dma_password = """\ncorrect horse battery staple\n"""'
        self.assertEqual(redact_secrets(line), line)

    def test_a_mixed_run_of_quotes_is_not_a_triple_delimiter(self):
        """A delimiter of `'""` could never close, so the value would run to
        the end of the line and take the fields after it along."""
        self.assertEqual(
            redact_secrets("{'password': '\"\"x', 'next': '/'}"),
            "{'password': 'REDACTED', 'next': '/'}",
        )

    def test_an_escaped_quote_does_not_close_the_value(self):
        """Escaping is the only way a JSON or TOML basic string carries its own
        delimiter, so a secret that holds one is still one value."""
        self.assertEqual(
            redact_secrets('{"password": "ab\\"cd", "x": 1}'),
            '{"password": "REDACTED", "x": 1}',
        )

    def test_a_password_or_api_key_value_is_covered(self):
        self.assertNotIn("hunter2", redact_secrets("password=hunter2"))
        self.assertNotIn("abc123", redact_secrets("api_key=abc123"))
        self.assertNotIn("abc123", redact_secrets("api-key=abc123"))

    def test_a_secret_value_is_covered(self):
        self.assertNotIn("abc123", redact_secrets("secret=abc123"))
        self.assertNotIn("abc123", redact_secrets("?client_secret=abc123"))

    def test_a_bearer_header_value_is_covered(self):
        out = redact_secrets("Authorization: Bearer s3cr3t")
        self.assertNotIn("s3cr3t", out)
        self.assertIn("Bearer REDACTED", out)

    def test_a_bare_key_or_sig_parameter_is_covered(self):
        """The spellings a signed media or feed URL uses. `-vv` releases the
        urllib3 loggers, whose per-request record carries the query string, so a
        user-supplied `file =` or RSS URL reaches both redacting destinations."""
        self.assertEqual(redact_secrets("?key=abc123&next=/"), "?key=REDACTED&next=/")
        self.assertEqual(redact_secrets("?sig=abc123&x=1"), "?sig=REDACTED&x=1")
        self.assertNotIn("deadbeef", redact_secrets("X-Amz-Signature=deadbeef"))
        self.assertNotIn("zzz", redact_secrets("signing_key=zzz"))

    def test_a_name_that_merely_ends_in_key_or_sig_is_left_alone(self):
        """The short names are why `\\w*` cannot front them: `sortkey` would be
        masked with the rest, and a masked diagnostic value reads as coverage
        while telling the reader nothing."""
        line = "?sortkey=date&hotkey=F1 monkey=1 sig_level=3 sigma=2 keys=3 keyboard=on"
        self.assertEqual(redact_secrets(line), line)


class RedactSourceLineTest(unittest.TestCase):
    """The malformed-line path. `redact_secrets` needs a value's bounds to mask
    it, and the lines this function is handed are exactly the ones a parser
    could not find bounds in — so every case here is a shape where a
    substitution having happened would have been the wrong question to ask."""

    def test_an_innocent_line_comes_back_verbatim(self):
        self.assertEqual(redact_source_line(["a = 1", "b = ?"], 2), ("b = ?", True))

    def test_a_secret_line_keeps_the_key_name_and_nothing_after_it(self):
        """The name says which setting the parser choked on, which is the whole
        diagnostic; past it there is no source text left to read."""
        self.assertEqual(
            redact_source_line(["[ultimate64]", 'dma_password = "hunter2"'], 2),
            ("dma_password REDACTED", False),
        )

    def test_a_doubled_equals_does_not_carry_the_value_through(self):
        """The #426 shape: `==` is what `redact_secrets` masks, so the
        passphrase survived *and* the caret was dropped — the one signal that
        the line had been protected fired while the credential was intact."""
        for line in (
            'dma_password == "hunter2"',
            'dma_password "hunter2"',
            'dma_password ""hunter2""',
            "dma_password : 'hunter2'",
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line(["[ultimate64]", line], 2)
                self.assertNotIn("hunter2", safe)
                self.assertIn("dma_password", safe)
                self.assertFalse(verbatim)

    def test_a_continuation_line_of_a_secret_value_is_dropped_whole(self):
        """The second #426 shape. The echoed line *is* the passphrase: the
        pattern keys on a key name and a continuation line carries none."""
        for delim in ('"""', "'''"):
            with self.subTest(delim=delim):
                lines = ["[ultimate64]", f"dma_password = {delim}", "correct horse", delim]
                safe, verbatim = redact_source_line(lines, 3)
                self.assertEqual(safe, "REDACTED")
                self.assertFalse(verbatim)
                self.assertNotIn("horse", safe)

    def test_a_closed_multiline_value_does_not_suppress_what_follows(self):
        """Only an *open* value reaches the rule — otherwise the first
        triple-quoted string in a file would blank every line after it."""
        lines = ["notes = '''", "prose", "'''", "b = ?"]
        self.assertEqual(redact_source_line(lines, 4), ("b = ?", True))

    def test_an_open_innocent_value_is_dropped_too(self):
        """Deliberately wider than the secret-shaped case: which key owns a
        continuation line needs a parse, and the parse is what failed."""
        self.assertEqual(redact_source_line(["notes = '''", "prose"], 2), ("REDACTED", False))

    def test_a_triple_quote_a_parser_never_saw_does_not_open_a_value(self):
        """A comment and a literal string can each carry a triple-quote run
        that opens nothing. Counting the runs instead of placing them flips the
        parity, so the real opening delimiter reads as a closing one and the
        continuation line carrying the passphrase comes back verbatim — #426
        again, one comment away."""
        triple = '"""'
        for noise in (f"# see {triple} for the multi-line form", f"notes = 'write {triple} here'"):
            with self.subTest(noise=noise):
                lines = [noise, "[ultimate64]", f"dma_password = {triple}", "correct horse"]
                self.assertEqual(redact_source_line(lines, 4), ("REDACTED", False))

    def test_a_continuation_line_of_an_open_array_is_dropped_too(self):
        """An array is an open value like any other: a line inside one carries
        no key name, so nothing on it can be attributed to a setting."""
        lines = ["[ultimate64]", "extra = [", '  "correct horse" bogus', "]"]
        self.assertEqual(redact_source_line(lines, 3), ("REDACTED", False))

    def test_a_closed_bracket_does_not_suppress_what_follows(self):
        """The bracket count has to come back down — a table header is two of
        them — or the first array in a file would blank every line after it."""
        for before in ("x = [1, 2]", "[ultimate64]", "x = { a = 1 }", "[[scenes]]"):
            with self.subTest(before=before):
                self.assertEqual(redact_source_line([before, "b = ?"], 2), ("b = ?", True))

    def test_a_password_in_a_url_is_dropped_though_its_key_is_not_secret_shaped(self):
        """`url` names no secret, so the key rule cannot fire, and the line is
        well formed enough that nothing is open — yet the value carries a
        password. `connect.redact_target` handles this once the target has
        parsed; the line reaching here is the one that did not."""
        for line in (
            'url = "u64://kelly:hunter2@192.168.2.64"',
            "url = 'u64://kelly:hunter2@192.168.2.64'  # trailing",
            'url = "http://kelly:hunter2@host/path?x=1"',
            'url = "u64://kelly@192.168.2.64"',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertNotIn("hunter2", safe)
                self.assertNotIn("kelly", safe)
                self.assertFalse(verbatim)
                self.assertTrue(safe.startswith("url = "), safe)

    def test_a_secret_key_later_on_the_line_does_not_carry_the_userinfo_through(self):
        """The key rule keeps the text up to the key name, so a password in a
        URL earlier on the same line rode out inside that prefix — the shape a
        signed feed URL with credentials has. The earlier cut has to win."""
        for line in (
            'url = "https://kelly:hunter2@host/feed?api_key=abc',
            'url = "https://kelly:hunter2@host/feed?sig=abc"',
            'opts = { url = "u64://kelly:hunter2@host", token = "t" }',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertNotIn("hunter2", safe)
                self.assertNotIn("kelly", safe)
                self.assertFalse(verbatim)

    def test_a_secret_key_before_the_userinfo_still_cuts_at_the_key(self):
        """The key name is the diagnostic, and it sits earlier than the URL, so
        truncating at the scheme would throw it away for nothing."""
        line = 'dma_password = "u64://kelly:hunter2@host"'
        self.assertEqual(redact_source_line([line], 1), ("dma_password REDACTED", False))

    def test_a_space_inside_the_userinfo_does_not_evade_the_rule(self):
        """A space is illegal in a URL, so stopping the netloc at one reads as
        defensible — but a passphrase with a space in it is exactly the shape
        that fails to parse and lands here, and it came back whole. The quote
        and `#` still bound the search, so nothing downwind of the value can
        pull the cut earlier."""
        for line in (
            'url = "u64://kelly:my pass@192.168.2.64',
            "url = 'u64://kelly:my pass@192.168.2.64'",
            'url = "u64://my user:my pass@192.168.2.64" bogus',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertNotIn("pass", safe)
                self.assertFalse(verbatim)
                self.assertTrue(safe.startswith("url = "), safe)

    def test_an_at_sign_outside_a_netloc_is_not_userinfo(self):
        """The netloc ends at the first `/`, `?` or `#`. A line truncated over
        an `@` in a path or a query would lose diagnostic text for nothing, and
        the rule would fire on ordinary config."""
        for line in (
            'url = "u64://192.168.2.64/path@v2"',
            'url = "https://host/feed?to=me@example.com"',
            'note = "mail me@example.com"',
            'path = "/tmp/a@b"',
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_source_line([line], 1), (line, True))


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


class ConfigureLoggingWiringTest(RestoresLogging):
    """`configure_logging` reconfigures the root logger and the held-back
    library loggers, so each test undoes all of it."""

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
