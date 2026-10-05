"""Tests for secret redaction on the durable and shared log destinations.

The split under test is deliberate and easy to regress in either direction: the
web console's token has to stay *intact* on the terminal, because the login URL
printed there is the only entry point a phone gets, and has to be *gone* from
`--log-file`, which outlives the run and is not created `0600`. The buffer half
of the same split is in `test_serve.py`, next to the buffer.

`ConfigureLoggingWiringTest` is the only class here that reconfigures the root
logger, and it undoes it: `configure_logging` clears the root handlers and
installs its own, and that outlives the test — the hazard
`_fakes.quiet_logging` exists for. So the end-to-end check drives a handler the
test owns outright, and the wiring check calls `configure_logging` under
`RestoresLogging` and inspects what it attached without emitting through it.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
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

    def test_an_akamai_token_loses_its_hmac(self):
        """Akamai signs a URL with `__token__=` or `hdnts=`, neither of which
        the key names reach (`__token__` has no word boundary after `token`).
        Its signature is the `hmac=` field inside the value; the expiry and
        path around it are not secret."""
        for name in ("__token__", "hdnts"):
            with self.subTest(name=name):
                self.assertEqual(
                    redact_secrets(f"https://cdn/a.mp3?{name}=exp=1~acl=/a/*~hmac=abc123&x=1"),
                    f"https://cdn/a.mp3?{name}=exp=1~acl=/a/*~hmac=REDACTED&x=1",
                )

    def test_a_secret_inside_a_url_encoded_value_is_covered(self):
        """`urlencode` turns the token's `=` into `%3D`, a Java-style encoder
        turns its `~` into `%7E` too, and a redirect URL carried in a query
        parameter spells `&sig=` as `%26sig%3D`. The escape's hex digit leaves
        no word boundary before the name, and the value ends at `%26`."""
        self.assertEqual(
            redact_secrets("hdnts=exp%3D1~acl%3D%2Fa%2F%2A~hmac%3Dabc123&x=1"),
            "hdnts=exp%3D1~acl%3D%2Fa%2F%2A~hmac%3DREDACTED&x=1",
        )
        self.assertEqual(
            redact_secrets("__token__=exp%3D1%7Eacl%3D%2F%7Ehmac%3Dabc123"),
            "__token__=exp%3D1%7Eacl%3D%2F%7Ehmac%3DREDACTED",
        )
        self.assertEqual(
            redact_secrets("u=https%3A%2F%2Fh%2F%3Fsig%3Ddeadbeef%26next%3D2"),
            "u=https%3A%2F%2Fh%2F%3Fsig%3DREDACTED%26next%3D2",
        )
        line = "u=%2Fmonkey%3D1%26sortkey%3Ddate"
        self.assertEqual(redact_secrets(line), line)

    def test_a_secret_inside_a_twice_encoded_value_is_covered(self):
        """A URL inside a parameter of a URL that is itself a parameter is
        encoded twice: `&sig=` becomes `%2526sig%253D`. Its value ends at
        `%2526`, or at the `%26` ending the middle URL's own parameter, while a
        once-encoded value keeps a `%2526` as part of the secret."""
        self.assertEqual(
            redact_secrets("u=a%3Fr%3Dh%253A%252F%252Fh%252F%253Fsig%253Ddeadbeef%2526n%253D2"),
            "u=a%3Fr%3Dh%253A%252F%252Fh%252F%253Fsig%253DREDACTED%2526n%253D2",
        )
        self.assertEqual(
            redact_secrets("u=a%3Fr%3D%253Ftoken%253Dabc%26n%3D2"),
            "u=a%3Fr%3D%253Ftoken%253DREDACTED%26n%3D2",
        )
        self.assertEqual(
            redact_secrets("u=%3Fsig%3Dabc%2526def%26n%3D2"),
            "u=%3Fsig%3DREDACTED%26n%3D2",
        )
        line = "u=%252Fmonkey%253D1%2526sortkey%253Ddate"
        self.assertEqual(redact_secrets(line), line)

    def test_an_encoded_colon_and_quote_read_as_their_raw_spellings(self):
        """A JSON document carried in a query parameter spells `"token":"v"`
        as `%22token%22%3A%22v%22`; with no encoded quote to end at, the value
        runs on to the `%26`."""
        self.assertEqual(
            redact_secrets("state=%7B%22token%22%3A%22abc%22%7D%26n%3D1"),
            "state=%7B%22token%22%3AREDACTED%26n%3D1",
        )
        self.assertEqual(
            redact_secrets("s=%257B%2527sig%2527%253A%2527abc%2527%257D"),
            "s=%257B%2527sig%2527%253AREDACTED",
        )
        line = "state=%7B%22monkey%22%3A%22abc%22%7D"
        self.assertEqual(redact_secrets(line), line)

    def test_a_name_that_merely_ends_in_key_or_sig_is_left_alone(self):
        """The short names are why `\\w*` cannot front them: `sortkey` would be
        masked with the rest, and a masked diagnostic value reads as coverage
        while telling the reader nothing."""
        line = "?sortkey=date&hotkey=F1 monkey=1 sig_level=3 sigma=2 keys=3 keyboard=on hmacs=1"
        self.assertEqual(redact_secrets(line), line)

    def test_a_prefix_led_by_dashes_is_still_kept_and_its_value_masked(self):
        """The prefixed names are tried from the start of a run of name
        characters, which may be a `-` rather than a word character."""
        for line, want in (
            ("--signing-key=zzz", "--signing-key=REDACTED"),
            ("-key=zzz x", "-key=REDACTED x"),
            ("a-b-c-x_sig=zzz", "a-b-c-x_sig=REDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_open_name_partway_through_a_dashed_run_is_still_found(self):
        """Only the short names are confined to the start of a run. `token`,
        `password`, `secret` and `apikey` are still tried at every word
        boundary, which is what reaches the one after a `-`. A `.` needs no
        such help: it ends the run, so the name after it starts a new one."""
        for line, want in (
            ("X-Auth-Token=zzz x", "X-Auth-Token=REDACTED x"),
            ("dma-password: zzz", "dma-password: REDACTED"),
            ("a.b-secret=zzz", "a.b-secret=REDACTED"),
            ("X-apikey=zzz", "X-apikey=REDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)
                safe, verbatim = redact_source_line([line], 1)
                self.assertNotIn("zzz", safe)
                self.assertFalse(verbatim)

    def test_a_long_dash_joined_run_is_redacted_in_linear_time(self):
        """A `-` puts a word boundary at every letter of `a-a-a-…`. A prefixed
        name tried from each of them scanned the rest of the run every time,
        which is quadratic: 10 KB of one took 1.5 s on a log line, and 100 KB
        took 139 s."""
        for line in ("a-" * 32_000, "key-" * 16_000, "x-" * 32_000 + "=1"):
            with self.subTest(line=line[:16]):
                start = time.perf_counter()
                redact_secrets(line)
                redact_source_line([line], 1)
                self.assertLess(time.perf_counter() - start, 2.0)


class RedactUrlUserinfoTest(unittest.TestCase):
    """A private media file is reached as `https://user:token@host/...`, and
    FFmpeg quotes the URL it failed on into the error every caller logs. The
    log file and the console's log tail redact at output, so the userinfo has
    to be one of the shapes they recognize."""

    def test_userinfo_is_masked(self):
        self.assertEqual(
            redact_secrets("open failed: 'https://alice:S3CRET@cdn.example/a.mp3'"),
            "open failed: 'https://REDACTED@cdn.example/a.mp3'",
        )

    def test_a_raw_at_sign_in_the_password_goes_too(self):
        self.assertEqual(redact_secrets("u64://kelly:p@ss@host/x"), "u64://REDACTED@host/x")

    def test_a_signature_on_the_same_url_is_masked_as_well(self):
        out = redact_secrets("https://a:b@cdn.example/v?sig=abc&x=1")
        self.assertEqual(out, "https://REDACTED@cdn.example/v?sig=REDACTED&x=1")

    def test_an_at_sign_outside_a_netloc_is_left_alone(self):
        for line in (
            "mail me@example.com",
            "https://host/feed?to=me@example.com",
            "tr://COM3 for kelly@host",
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), line)

    def test_masking_is_idempotent(self):
        once = redact_secrets("https://alice:S3CRET@cdn.example/a.mp3")
        self.assertEqual(redact_secrets(once), once)

    def test_a_quote_in_the_password_goes_too(self):
        """RFC 3986 allows a raw `'` in userinfo, so the URL is well formed and
        FFmpeg opens it; a quote that bounded the match left it whole."""
        for line, want in (
            ("https://alice:it's@cdn.example/a.mp3", "https://REDACTED@cdn.example/a.mp3"),
            ('"https://alice:it\'s@cdn.example/a.mp3"', '"https://REDACTED@cdn.example/a.mp3"'),
            # A TOML basic string's `\"` parses to a raw `"` in the URL opened.
            ('https://alice:it"s@cdn.example/a.mp3', "https://REDACTED@cdn.example/a.mp3"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_userinfo_reaching_into_the_next_string_does_not_strand_a_secret(self):
        """No quote bounds the userinfo, so on a run with no whitespace it
        reaches from a path-less URL into the next string. Masked first, it
        swallowed that string's key name and left whatever followed the
        secret's own `@` with nothing to name it."""
        for line, want in (
            ('{"url":"u64://192.168.2.64","dma_password":"hunt@er2"}', '{"url":"u64://REDACTED"}'),
            ('["tr://COM3","token=abc@def"]', '["tr://REDACTED"]'),
            ("{'url':'u64://h','viewer_token':'ab@cd'}", "{'url':'u64://REDACTED'}"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_value_the_userinfo_cut_short_is_masked_to_its_real_end(self):
        """Read as given, the `'` in the password closes the token's value;
        once the userinfo is masked the value runs on to its real quote."""
        self.assertEqual(redact_secrets("token='https://u:it's@h/a.mp4'"), "token='REDACTED'")

    def test_a_long_run_of_scheme_characters_is_redacted_in_linear_time(self):
        """Every line `--log-file` and the console's log tail receive goes
        through here. A scheme pattern retried from every offset of a run of
        scheme characters is quadratic in the run: 64 KB of hex took 11 s, and
        the same run on a config line the parser refused took 17 s to quote."""
        for line in ("a" * 64_000, "deadbeef0123" * 5_000, "x://" + "a" * 64_000):
            with self.subTest(line=line[:16]):
                start = time.perf_counter()
                self.assertEqual(redact_secrets(line), line)
                redact_source_line([line], 1)
                self.assertLess(time.perf_counter() - start, 2.0)


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

    def test_the_cut_falls_where_the_scheme_begins(self):
        """The cut keeps the key name and drops the whole URL, scheme
        included, so nothing of the target is left to piece together."""
        for line in ('url = "u64://kelly:hunter2@host"', "url=u64+x://kelly:hunter2@host"):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertEqual(safe, line[: line.index("u64")] + "REDACTED")
                self.assertFalse(verbatim)

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
        that fails to parse and lands here, and it came back whole."""
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

    def test_a_quote_inside_the_userinfo_does_not_evade_the_rule(self):
        """RFC 3986 allows a raw `'` in userinfo. In a literal string it ends
        the value early, so the parser rejects that very line and it lands
        here; a `'` that bounded the search echoed the password whole. A `"`
        is not legal in a URL, but it is the same shape in a basic string: the
        password's own quote ends the value, or an escaped one rides through
        on a line refused for something else."""
        for line in (
            "file = 'https://kelly:it's@cdn.example/a.mp4'",
            'file = "https://kelly:it\'s@cdn.example/a.mp4" bogus',
            'file = "https://kelly:it"s@cdn.example/a.mp4"',
            'file = "https://kelly:it\\"s@cdn.example/a.mp4" bogus',
            'file = """https://kelly:it"s@cdn.example/a.mp4""" bogus',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertEqual(safe, line[: line.index("https")] + "REDACTED")
                self.assertFalse(verbatim)

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
