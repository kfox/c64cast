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
from collections.abc import Callable

from _fakes import RestoresLogging

from c64cast._redact import _NOT_A_PASSWORD_REACH, redact_secrets, redact_source_line
from c64cast.app import cli_commands

LOGIN_LINE = "web console: open http://127.0.0.1:8123/api/login?token=s3cr3t&next=/"


def _hidden_value_ladder(levels: int, filler: int) -> str:
    """`levels` hidden values, each one separator level shallower than the
    last, then `filler` characters, then the encoded `&`s that end them
    deepest first, so each value runs on past the one before it."""
    return (
        "".join(
            f"token%{'25' * (levels + 1)}3Dpassword%{'25' * e}3Dx%{'25' * (levels + 1)}26"
            for e in range(levels - 1, -1, -1)
        )
        + "y" * filler
        + "".join(f"%{'25' * d}26" for d in range(levels, -1, -1))
    )


#: How many times longer the long input of a linear-time check is than its
#: short one.
_SCALE = 4

#: Measurements a linear-time check takes of each input.
_TRIES = 3


#: CPU seconds a measurement runs `work` for before dividing by the runs. A
#: single call is not timed alone: Windows advances a thread's CPU clock once
#: per 15.6 ms tick, so a call of a few milliseconds reads as zero there.
_MEASURE_S = 0.1


def _cpu_seconds(work: Callable[[str], object], line: str) -> float:
    runs = 0
    started = time.thread_time()
    while True:
        work(line)
        runs += 1
        spent = time.thread_time() - started
        if spent >= _MEASURE_S:
            return spent / runs


def _redacts_both_ways(line: str) -> None:
    redact_secrets(line)
    redact_source_line([line], 1)


def _assert_linear_time(
    test: unittest.TestCase,
    make: Callable[[int], str],
    work: Callable[[str], object] = _redacts_both_ways,
) -> None:
    """Fail unless `work` takes time linear in the length of `make(scale)`.

    Compares `make(1)` against `make(_SCALE)`, each `_SCALE` times longer.
    A linear pass spends about `_SCALE` times as long on the long input and a
    quadratic one about `_SCALE` squared, so the check allows twice the length
    ratio. A wall-clock limit on one input fails whenever the machine is
    loaded; a ratio between two inputs measured on the same machine does not.
    The clock is this thread's CPU time, which stops while the scheduler runs
    something else. Each input keeps its fastest of `_TRIES` measurements, all
    taken before the ratio is judged: deciding after each try would let one
    inflated measurement of the short input pass a quadratic regression.
    """
    short, long = make(1), make(_SCALE)
    allowed = 2 * len(long) / len(short)
    fastest_short = fastest_long = float("inf")
    for _ in range(_TRIES):
        fastest_short = min(fastest_short, _cpu_seconds(work, short))
        fastest_long = min(fastest_long, _cpu_seconds(work, long))
    if fastest_long <= allowed * fastest_short:
        return
    test.fail(
        f"{len(long) / len(short):.1f}x the input took "
        f"{fastest_long / fastest_short:.1f}x the time "
        f"({fastest_short * 1000:.1f} ms, then {fastest_long * 1000:.1f} ms)"
    )


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

    def test_an_escaped_quote_followed_by_a_non_word_does_not_close_the_value(self):
        """The quote after a letter never closes a value anyway, so the escape
        only matters where a space, comma or brace follows it."""
        for line, want in (
            ('password="ab\\" cd"', 'password="REDACTED"'),
            ("token='a\\' b'", "token='REDACTED'"),
            ('{"password": "ab\\", cd", "x": 1}', '{"password": "REDACTED", "x": 1}'),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_password_or_api_key_value_is_covered(self):
        self.assertNotIn("hunter2", redact_secrets("password=hunter2"))
        self.assertNotIn("abc123", redact_secrets("api_key=abc123"))
        self.assertNotIn("abc123", redact_secrets("api-key=abc123"))

    def test_a_secret_value_is_covered(self):
        self.assertNotIn("abc123", redact_secrets("secret=abc123"))
        self.assertNotIn("abc123", redact_secrets("?client_secret=abc123"))

    def test_a_camera_url_password_is_covered(self):
        """A Foscam-style IP camera takes its login in the query string, and a
        failed video open quotes the URL into its error."""
        self.assertEqual(
            redact_secrets("http://cam/videostream.cgi?user=admin&pwd=hunter2&res=0"),
            "http://cam/videostream.cgi?user=admin&pwd=REDACTED&res=0",
        )
        self.assertEqual(
            redact_secrets("http://cam/videostream.cgi?loginuse=admin&loginpas=hunter2"),
            "http://cam/videostream.cgi?loginuse=admin&loginpas=REDACTED",
        )

    def test_each_password_and_credential_name_masks_its_value(self):
        forms = (
            "http://h/a?u=1&{name}=S3CR&n=1",
            "{name}=S3CR",
            "{name}: S3CR",
            '{{"{name}": "S3CR", "x": 1}}',
            "db_{name}=S3CR",
            "X-{name}: S3CR",
            "%26{name}%3DS3CR",
        )
        for name in (
            "pwd",
            "passwd",
            "password",
            "passphrase",
            "passcode",
            "loginpas",
            "loginpass",
            "pass",
            "auth",
            "jwt",
            "credential",
            "credentials",
        ):
            for form in forms:
                line = form.format(name=name)
                with self.subTest(line=line):
                    self.assertNotIn("S3CR", redact_secrets(line))
            with self.subTest(source_line=name):
                safe, verbatim = redact_source_line([f'{name} == "S3CR"'], 1)
                self.assertNotIn("S3CR", safe)
                self.assertFalse(verbatim)

    def test_a_glued_prefix_takes_the_long_names_and_pass_but_not_auth(self):
        """`passwd`, `pwd`, `jwt`, `credential` and `pass` are open names, like
        `password`, bar the words that end in `pass` and name nothing secret;
        `auth` is a short one, like `key`, because glued it is `oauth`. A
        handful of glued `…key` names are secrets too, where `sortkey` and
        `monkey` are not."""
        for line, want in (
            ("dbpasswd=x", "dbpasswd=REDACTED"),
            ("wifipassphrase=x", "wifipassphrase=REDACTED"),
            ("adminpasscode=x", "adminpasscode=REDACTED"),
            ("userpwd=x", "userpwd=REDACTED"),
            ("idjwt=x", "idjwt=REDACTED"),
            ("awscredentials=x", "awscredentials=REDACTED"),
            ("userpass=x", "userpass=REDACTED"),
            ("authkey=x", "authkey=REDACTED"),
            ("streamkey=x", "streamkey=REDACTED"),
            ("bypass=on", "bypass=on"),
            ("oauth=1", "oauth=1"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_word_that_merely_contains_a_credential_name_is_left_alone(self):
        """A name masks only when the key ends where the name does, so
        ordinary diagnostic text keeps its values. `oauth_state` is kept on
        purpose: it ends in `state`, and an OAuth credential travels as
        `oauth_token`, which `token` covers."""
        line = (
            "passes=3 bypass=on compass: north author=Kelly authority: x "
            "oauth_state=abc pass_count=2 jwt_expiry_s=30 passing: yes "
            "authed=1 pwdx=1 jwts=1 credentialed=1 passwords=4 "
            "passphrases=2 passcodes=2 loginpassed=1"
        )
        self.assertEqual(redact_secrets(line), line)

    def test_a_word_ending_in_pass_that_names_no_secret_keeps_its_value(self):
        """Audio filters and encoder passes end in `pass` and are not
        passwords, in any spelling; `PWD` is the shell's working directory,
        while a camera URL's `pwd=` is lowercase."""
        line = (
            "highpass=200 high-pass=200 low_pass=8k bandpass=1 bypass_audio_lock=1 "
            "two_pass=1 compass=n PWD=/home/x OLDPWD=/tmp"
        )
        self.assertEqual(redact_secrets(line), line)
        self.assertEqual(redact_secrets("pwd=x Pwd=y"), "pwd=REDACTED Pwd=REDACTED")

    def test_an_excluded_word_counts_only_from_the_start_of_a_component(self):
        """`firewall_pass` joined is `firewallpass`, which ends in `allpass`,
        and `phone_pass` ends in `onepass`; neither names a filter or an
        encoder pass, so both keep their mask."""
        for name in (
            "firewall_pass",
            "phone_pass",
            "lobby_pass",
            "broadband_pass",
            "failover-pass",
            "telecom_pass",
            # A run past the reach: the window opens on the `by` of `qby`,
            # which only looks like the start of `bypass`.
            "qby" + "_" * (_NOT_A_PASSWORD_REACH - 2) + "pass",
        ):
            with self.subTest(name=name):
                self.assertEqual(redact_secrets(f"{name}=hunter2"), f"{name}=REDACTED")
                safe, verbatim = redact_source_line([f'{name} = "hunter2'], 1)
                self.assertNotIn("hunter2", safe)
                self.assertFalse(verbatim)

    def test_a_quote_inside_a_one_word_value_does_not_end_it(self):
        """A quote that a letter or digit follows is part of the value: the
        `'` in a password `it's@er2` ended the value early and left the rest,
        as did the `}` inside a Bearer token and the `b` of a bytes repr."""
        for line, secret in (
            ("x=\"u64://h\",token='it's@er2'", "er2"),
            ("token=it's@er2 next", "er2"),
            ("Bearer s3}cr3t", "cr3t"),
            ("token: b's3cr3t'", "s3cr3t"),
        ):
            with self.subTest(line=line):
                self.assertNotIn(secret, redact_secrets(line))
        self.assertEqual(redact_secrets("token: b's3cr3t' x"), "token: b'REDACTED' x")

    def test_letters_before_a_quote_are_a_prefix_only_when_python_takes_them(self):
        """`rU` is no string prefix, and a `b` whose quote never closes is not
        known to be one, so the letters are the secret's own and go too."""
        for line, want in (
            ("token=rU'secretvalue", "token=REDACTED"),
            ('token=bU"secretvalue" more', 'token=REDACTED" more'),
            ("token=b'secretvalue", "token=REDACTED"),
            ("token=rb'secretvalue' x", "token=rb'REDACTED' x"),
            ("token=rU'abc def' x", "token=REDACTED' x"),
            ("Bearer rU'abc def' x", "Bearer REDACTED' x"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_arrow_or_a_doubly_escaped_rendering_still_separates(self):
        """A Ruby or PHP hash spells the separator `=>`, and JSON quoted
        inside JSON escapes its quotes more than once."""
        for line, want in (
            ("{'token' => 'abc'}", "{'token' => 'REDACTED'}"),
            ("{'token'=>'abc'}", "{'token'=>'REDACTED'}"),
            ("password=>abc123 x", "password=REDACTED x"),
            ("{\\'token\\'=>\\'abc\\'}", "{\\'token\\'=>\\'REDACTED'}"),
            (
                '"{\\\\\\"token\\\\\\": \\\\\\"abc\\\\\\"}"',
                '"{\\\\\\"token\\\\\\": \\\\\\"REDACTED"}"',
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_quoted_value_holding_prose_ends_at_its_first_quote(self):
        """A value that has held whitespace is prose, and a possessive's `'`
        would otherwise carry the mask on to the next quote a space follows,
        however far down the line that is."""
        line = "token=' x secret=\"a'bcdef\" kept, and the user's name stays"
        out = redact_secrets(line)
        self.assertNotIn("bcdef", out)
        self.assertIn("kept, and the user's name stays", out)

    def test_a_name_quoted_inside_another_value_reaches_its_own_quote(self):
        """A name inside a quoted value can open a value of its own with an
        encoded or escaped quote, and that value runs on to the matching one
        past the outer value's end."""
        for line in (
            'sig="x token:%27a" S3CR%27',
            "sig=\"x token=\\'a\" S3CR'",
            "token=' x secret=\"a'bcdef\"",
        ):
            with self.subTest(line=line):
                out = redact_secrets(line)
                self.assertNotIn("S3CR", out)
                self.assertNotIn("bcdef", out)

    def test_any_scheme_in_an_authorization_value_loses_its_credential(self):
        """The scheme is kept, as `Bearer` is, and whatever follows it is the
        credential — `Basic`, GitHub's `token`, or a quoted Bearer value."""
        for line, want in (
            ("Authorization: token ghp_x", "Authorization: token REDACTED"),
            ("Authorization: Basic dXNlcjpw", "Authorization: Basic REDACTED"),
            ('Authorization: Bearer "s3cr3t"', 'Authorization: Bearer "REDACTED"'),
            ("{'Authorization': 'Basic dXNlcjpw'}", "{'Authorization': 'Basic REDACTED'}"),
            ("Proxy-Authorization: Digest abc", "Proxy-Authorization: Digest REDACTED"),
            ("Authorization: s3cr3t", "Authorization: REDACTED"),
            (
                "h=Authorization:%20Bearer%20ab+cd/ef==",
                "h=Authorization:%20Bearer%20REDACTED",
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_first_word_that_is_no_known_scheme_goes_with_the_credential(self):
        """A bare credential followed by more text reads as a scheme and its
        credential, so only a registered scheme is kept in view."""
        for line, want in (
            ("Authorization: s3cr3t rejected by host", "Authorization: REDACTED by host"),
            ("{'Authorization': 's3cr3t def'}", "{'Authorization': 'REDACTED'}"),
            ("Authorization: SSWS s3cr3t", "Authorization: REDACTED"),
            ("Authorization: NEGOTIATE s3cr3t", "Authorization: NEGOTIATE REDACTED"),
            ('Authorization: s3cr3t "" x', 'Authorization: REDACTED "" x'),
            ("Authorization: s3cr3t ''", "Authorization: REDACTED ''"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_bearer_as_a_key_masks_its_value(self):
        """Followed by a separator rather than a space, `bearer` is a key."""
        for line, want in (
            ("bearer=s3cr3t", "bearer=REDACTED"),
            ('{"bearer": "s3cr3t"}', '{"bearer": "REDACTED"}'),
            ("x_bearer: s3cr3t", "x_bearer: REDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_bearer_credential_is_read_whatever_it_starts_with(self):
        """The first character after the scheme is always part of the
        credential, so a stray `,` does not leave it with nothing to mask, and
        a `Bearer` glued to a prefix or doubled still masks what follows."""
        for line, want in (
            ("Bearer ,s3cr3t", "Bearer REDACTED"),
            ("Bearer _Bearer s3cr3t", "Bearer REDACTED REDACTED"),
            ("x_Bearer s3cr3t", "x_Bearer REDACTED"),
            ("Bearer%20Bearer%20abc", "Bearer%20REDACTED%20REDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_encoded_space_ends_a_credential_only_at_its_own_depth(self):
        """`%20` separates a once-encoded `Bearer` from its credential, so a
        `%2520` after it is a space inside the credential, encoded once more;
        an encoded tab or newline separates as a space does."""
        for line, want in (
            ("Bearer%20abc%2520def", "Bearer%20REDACTED"),
            ("Bearer%20abc%20def", "Bearer%20REDACTED%20def"),
            ("Bearer%09abc", "Bearer%09REDACTED"),
            ("Bearer%0Aabc", "Bearer%0AREDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_escape_assembled_from_other_escapes_is_decoded_too(self):
        """Decoding runs to a fixed point: `%25%37%34` is `%74` once decoded,
        and that is `t`, so the line names `token`."""
        self.assertEqual(redact_secrets("%25%37%34oken=abc"), "%25%37%34oken=REDACTED")

    def test_a_bearer_header_value_is_covered(self):
        out = redact_secrets("Authorization: Bearer s3cr3t")
        self.assertNotIn("s3cr3t", out)
        self.assertIn("Bearer REDACTED", out)

    def test_a_bearer_token_under_a_secret_name_is_covered(self):
        """A secret name's unquoted value ends at the space after `Bearer`, so
        that word alone was masked and the token behind it was not — the same
        whether the name is raw or found inside an encoded value."""
        for line, want in (
            ("access_token: Bearer s3cr3t", "access_token: REDACTED REDACTED"),
            ("u=%2526token%253D Bearer s3cr3t", "u=%2526token%253D REDACTED REDACTED"),
            ("key: Bearer Bearer s3cr3t", "key: REDACTED REDACTED REDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_name_inside_a_bearer_value_does_not_hide_a_later_one(self):
        """A name inside a Bearer value starts a match once the two rules are
        searched apart, and its quoted value can close inside a later name's
        quoted value: `bcdef` came back whole."""
        for line in (
            "Bearer token=' x secret=\"a'bcdef\"",
            "u64://h:p@h Bearer token=' x secret=\"a'bcdef\"",
        ):
            with self.subTest(line=line):
                self.assertNotIn("bcdef", redact_secrets(line))

    def test_a_bearer_after_a_percent_escape_is_covered(self):
        """The escape's hex digit leaves no word boundary before `Bearer`, and
        a secret name's value ends at the space after it, so the token behind
        an encoded separator was masked by neither rule. An encoded header
        spells the space `%20` or `+`, and its value ends at the next one."""
        for line, want in (
            ("%22token%22%3ABearer abc", "%22token%22%3AREDACTED REDACTED"),
            ("token%3aBearer abc", "token%3aREDACTED REDACTED"),
            ("%2522token%2522%253ABearer abc", "%2522token%2522%253AREDACTED REDACTED"),
            ("x%3DBearer abc", "x%3DBearer REDACTED"),
            ("h=Authorization%3A%20Bearer%20abc%20x", "h=Authorization%3A%20Bearer%20REDACTED%20x"),
            (
                "h=Authorization%253A%2520Bearer%2520abc",
                "h=Authorization%253A%2520Bearer%2520REDACTED",
            ),
            ("h=Authorization:+Bearer+abc+x", "h=Authorization:+Bearer+REDACTED+x"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_long_run_of_encoded_bearers_is_redacted_in_linear_time(self):
        """Every other `Bearer` in the run starts a match, and the one between
        is its value (the `%20` a match consumes leaves that one no escape to
        follow). A value that ran past an encoded space read the rest of the run
        once per match, which is quadratic: 144 KB of `Bearer%20` took 3.6 s."""
        _assert_linear_time(self, lambda s: "Bearer%20" * 4_000 * s, redact_secrets)

    def test_a_bare_key_or_sig_parameter_is_covered(self):
        """The spellings a signed media or feed URL uses. `-vv` releases the
        urllib3 loggers, whose per-request record carries the query string, so a
        user-supplied `file =` or RSS URL reaches both redacting destinations."""
        self.assertEqual(redact_secrets("?key=abc123&next=/"), "?key=REDACTED&next=/")
        self.assertEqual(redact_secrets("?sig=abc123&x=1"), "?sig=REDACTED&x=1")
        self.assertNotIn("deadbeef", redact_secrets("X-Amz-Signature=deadbeef"))
        self.assertNotIn("zzz", redact_secrets("signing_key=zzz"))

    def test_a_json_escaped_separator_before_a_short_name_still_separates(self):
        """A URL inside a JSON string spells `&` as `\\u0026`, so the escape's
        last hex digit sits against `sig` and the name read as glued to a word.
        An escape of a letter is still a word: `\\u0061key` is `akey`."""
        for line, expected in (
            (
                '"https://h/x?a=1\\u0026sig=abc123 rest"',
                '"https://h/x?a=1\\u0026sig=REDACTED rest"',
            ),
            ("x\\u002Chmac=abc123 rest", "x\\u002Chmac=REDACTED rest"),
            ("a\\\\u0026key=abc123 rest", "a\\\\u0026key=REDACTED rest"),
            ("x\\u0061key=abc123 rest", "x\\u0061key=abc123 rest"),
            ("x\\u0026signal=1 rest", "x\\u0026signal=1 rest"),
            ("x\\u005fkey=abc123 rest", "x\\u005fkey=REDACTED rest"),
            ("x\\u002Dhmac=abc123 rest", "x\\u002Dhmac=REDACTED rest"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), expected)

    def test_a_flag_spelled_with_a_space_masks_the_next_word(self):
        """A logged command line gives a flag its value as the next word:
        yt-dlp's `--password` and `--video-password`. A next word that is a
        flag of its own is not the value, and a name that is no flag's keeps
        what follows it."""
        for line, expected in (
            ("yt-dlp --password hunter2 --verbose", "yt-dlp --password REDACTED --verbose"),
            ("x --video-password hunter2 y", "x --video-password REDACTED y"),
            ("x -token hunter2 y", "x -token REDACTED y"),
            ('x --password "a b" y', 'x --password "REDACTED" y'),
            ("x --api-key\thunter2 y", "x --api-key\tREDACTED y"),
            ("x --password --verbose", "x --password --verbose"),
            ("x --password", "x --password"),
            ("the password is hunter2", "the password is hunter2"),
            ("x --password-file path y", "x --password-file path y"),
            ("x --key 3.0:r y", "x --key 3.0:r y"),
            ("set C=-key pause", "set C=-key pause"),
            ("x --stream-key s3cret y", "x --stream-key REDACTED y"),
            ("x --secret-key s3cret y", "x --secret-key REDACTED y"),
            ("x --signing-key s3cret y", "x --signing-key REDACTED y"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), expected)

    def test_a_json_escaped_separator_starts_an_exempt_word(self):
        """The exempt words read the escape as the separator it spells, as the
        short names do: `\\u0026bypass=` is `&bypass=`, not `u0026bypass=`."""
        for line in (
            "x\\u0026bypass=1 rest",
            "x\\u0026high-pass=1 rest",
            "x\\u005fbypass=1 rest",
            "x\\u002dbypass=1 rest",
            "x\\u0026PWD=/home/k rest",
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), line)
        self.assertNotIn("abc", redact_secrets("x\\u0061bypass=abc rest"))
        self.assertNotIn("abc", redact_secrets("x\\u0061PWD=abc rest"))

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

    def test_a_secret_encoded_any_number_of_times_is_covered(self):
        """Each level of encoding puts one more `25` after every `%`. A value
        encoded three times ends at an `&` of any shallower level, and keeps
        its own `&`, which is `%25252526` there."""
        for line, want in (
            (
                "u=%252526sig%25253Dab%25252526cd%252526n%25253D2",
                "u=%252526sig%25253DREDACTED%252526n%25253D2",
            ),
            ("u=%252526token%25253Dab%2526n", "u=%252526token%25253DREDACTED%2526n"),
            ("u=%25257Ehmac%25253Dab%26n", "u=%25257Ehmac%25253DREDACTED%26n"),
            ("s=%25252522sig%25252522%2525253Aab", "s=%25252522sig%25252522%2525253AREDACTED"),
            ("u=%25252526sig%2525253Dab", "u=%25252526sig%2525253DREDACTED"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)
        line = "u=%25252Fmonkey%25253D1%252526sortkey%25253Ddate"
        self.assertEqual(redact_secrets(line), line)

    def test_an_encoded_colon_and_quote_read_as_their_raw_spellings(self):
        """A JSON document carried in a query parameter spells `"token":"v"`
        as `%22token%22%3A%22v%22`. Decoded, that is a quoted value like any
        other, and it ends at its own closing quote rather than taking the
        rest of the document with it."""
        self.assertEqual(
            redact_secrets("state=%7B%22token%22%3A%22abc%22%7D%26n%3D1"),
            "state=%7B%22token%22%3A%22REDACTED%22%7D%26n%3D1",
        )
        self.assertEqual(
            redact_secrets("s=%257B%2527sig%2527%253A%2527abc%2527%257D"),
            "s=%257B%2527sig%2527%253A%2527REDACTED%2527%257D",
        )
        line = "state=%7B%22monkey%22%3A%22abc%22%7D"
        self.assertEqual(redact_secrets(line), line)

    def test_a_quote_deeper_than_the_separator_is_part_of_the_value(self):
        """A percent-encoded password may hold a quote. Behind a raw `=`, a
        `%22` read only as an opener ended the mask at the next `%22` and the
        rest of the password stayed in view — from a value, a `Bearer`
        credential, and an `Authorization` value alike."""
        for line, want in (
            (
                "GET /cam?u=a&pwd=R%22ab%22-tail&x=1 HTTP/1.1",
                "GET /cam?u=a&pwd=REDACTED&x=1 HTTP/1.1",
            ),
            ("/cam?pwd=b%27ab%27.tail&x=1", "/cam?pwd=REDACTED&x=1"),
            ("/cam?pwd=%27ab%27%26tail&x=1", "/cam?pwd=REDACTED&x=1"),
            ("/cam?pwd=%22ab%22%20tail x", "/cam?pwd=REDACTED x"),
            ("/x?pwd=%5C%22ab%5C%22-tail&x=1", "/x?pwd=REDACTED&x=1"),
            ("Bearer %22ab%22-tail rest", "Bearer REDACTED rest"),
            ("Authorization: Basic %27ab%27-tail rest", "Authorization: Basic REDACTED rest"),
            ("Authorization: %22ab%22-tail rest", "Authorization: REDACTED rest"),
            ("Authorization: %22Basic ab%22-tail rest", "Authorization: %22Basic REDACTED rest"),
            ("Authorization: (Basic ab) rest", "Authorization: (Basic REDACTED rest"),
            ("Authorization: (Basic) ab rest", "Authorization: (Basic) REDACTED rest"),
            ("Authorization: Basic, ab rest", "Authorization: Basic, REDACTED rest"),
            ("Authorization: s3cr3t, ab rest", "Authorization: REDACTED rest"),
            ("Authorization: %22Basic%22 ab rest", "Authorization: %22Basic%22 REDACTED rest"),
            ("Authorization: %22s3cr3t%22, ab rest", "Authorization: REDACTED rest"),
            ("Authorization: %22Basic a b%22-tail rest", "Authorization: %22Basic REDACTED rest"),
            ("Authorization: %22s3 a b%22-tail rest", "Authorization: REDACTED rest"),
            ("Authorization: b%22Basic%22 ab rest", "Authorization: b%22Basic%22 REDACTED rest"),
            (
                "Authorization: rb%5C%22Bearer%5C%22 ab rest",
                "Authorization: rb%5C%22Bearer%5C%22 REDACTED rest",
            ),
            ("token: rU%27a b%27-tail rest", "token: REDACTED rest"),
            ("Bearer rU%22ab cd%22-tail rest", "Bearer REDACTED rest"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_name_inside_another_names_value_keeps_its_value_masked(self):
        """A search resumes where a match ends, so a name inside a value
        starts no match of its own. Its value still outlasted the one it hid
        in when its separator began where that value ended — at a space or a
        quote — or when an encoded `&` ended the outer value and the hidden
        name's separator was shallower than that `&` — or when the outer
        value was quoted and the hidden one was quoted with another kind."""
        for line, want in (
            ("sig=\"x token:'a\" S3CR'", "sig=\"REDACTED'"),
            ("sig=\"x 'token': 'a\" S3CR'", "sig=\"REDACTED'"),
            ("sig%25253D'''x token:\"a''' S3CR\"", "sig%25253D'''REDACTED\""),
            ('-sig%2525253d"""a-sig:\'\'\'"""" S3CR', '-sig%2525253d"""REDACTED'),
            ("token%3Apassword =S3CR", "token%3AREDACTED =REDACTED"),
            ("sig%253atoken%253d %2FS3CR[", "sig%253aREDACTED REDACTED"),
            ("token%3Apassword'=S3CR", "token%3AREDACTED'=REDACTED"),
            ("token%3AX-Auth-Token =S3CR", "token%3AREDACTED =REDACTED"),
            ("token%3A%22key%22 =S3CR", "token%3A%22REDACTED%22 =REDACTED"),
            ("%22token%22%3Apassword%22%3A S3CR", "%22token%22%3AREDACTED%22%3A REDACTED"),
            ("token=password =S3CR", "token=REDACTED =REDACTED"),
            ("token%3Apassword=a%26S3CR", "token%3AREDACTED"),
            ("token%253Apassword%3Da%2526S3CR x", "token%253AREDACTED x"),
            (
                "token%3Dpassword=a%26b c token%3Dpassword=d%26S3CR",
                "token%3DREDACTED c token%3DREDACTED",
            ),
            (
                "token%25253Dpassword%253Dx%25252526token%25253Dpassword%3Dx%25252526"
                "S3CR%252526S3CR%2526S3CR%26tail",
                "token%25253DREDACTED%26tail",
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)
        for line in ("token%3Ax%26n%3D1 y=2", "token%3Amonkey=1%26n%3D1", "token%3Ax y"):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line).count("REDACTED"), 1)

    def test_a_hidden_value_ladder_masks_the_filler(self):
        """Each rung's value runs on past the `&`s deeper than its separator,
        so the shallowest one reaches through the filler to the last `%26`."""
        self.assertNotIn("y", redact_secrets(_hidden_value_ladder(50, 5_000)))

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
        for make in (
            lambda s: "a-" * 8_000 * s,
            lambda s: "key-" * 4_000 * s,
            lambda s: "pass-" * 4_000 * s,
            lambda s: "pwd-" * 4_000 * s,
            lambda s: "x-" * 8_000 * s + "=1",
        ):
            with self.subTest(line=make(1)[:16]):
                _assert_linear_time(self, make)

    def test_a_long_run_of_encoded_percent_signs_is_redacted_in_linear_time(self):
        """An escape is read as `%`, any run of `25`, then its digits. Given
        back a pair at a time, that run rescans the name characters after it
        once per pair, which is quadratic: 32 KB of `%2525…` took 16 s."""
        for make in (
            lambda s: "%" + "25" * 2_000 * s + "a" * 4_000 * s,
            lambda s: "%" + "25" * 8_000 * s + "token",
            lambda s: "%" + "25" * 8_000 * s + "pwd",
            lambda s: "%" + "25" * 8_000 * s + "pass",
        ):
            with self.subTest(line=make(1)[:16]):
                _assert_linear_time(self, make)


def _nested_escape(scale: int) -> str:
    """`%253` repeated in front of `%34`: each decoding assembles the next escape."""
    line = "%34"
    while len(line) < 32_000 * scale:
        line = "%253" + line
    return line


def _linear_time_tests(
    shapes: dict[str, Callable[[int], str]],
    work: Callable[[str], object] = _redacts_both_ways,
) -> Callable[[type[unittest.TestCase]], type[unittest.TestCase]]:
    """Add a `test_<name>` to the decorated class for each of `shapes`.

    One test per shape rather than one test looping over them: each
    `_assert_linear_time` takes about a second of CPU, and the per-test cap
    applies to wall time, which a loaded machine stretches. Thirty shapes in
    one test took over eight seconds on an idle machine.
    """

    def add(cls: type[unittest.TestCase]) -> type[unittest.TestCase]:
        for name, make in shapes.items():

            def test(self: unittest.TestCase, make: Callable[[int], str] = make) -> None:
                _assert_linear_time(self, make, work)

            test.__name__ = f"test_{name}"
            test.__qualname__ = f"{cls.__qualname__}.{test.__name__}"
            if hasattr(cls, test.__name__):
                raise TypeError(f"{cls.__qualname__} already has {test.__name__}")
            setattr(cls, test.__name__, test)
        return cls

    return add


@_linear_time_tests(
    {
        "encoded_name_over_names_and_filler": lambda s: (
            "token%3A" + "token=" * 4_000 * s + "%26" + "a" * 24_000 * s + " "
        ),
        "repeated_encoded_names": lambda s: "token%3A" * 4_000 * s + "%26",
        "double_encoded_pairs": lambda s: "token%253Apassword=x%2526" * 1_500 * s + " ",
        "encoded_name_password_pairs": lambda s: "token%3Apassword " * 2_000 * s,
        "pwd_pass_pairs": lambda s: "pwd%3Apass " * 2_000 * s,
        "quoted_sig_over_token_quotes": lambda s: 'sig="' + "token:'" * 2_000 * s + '"',
        "alternating_quoted_sigs": lambda s: ('sig="x token:\'"' + "sig='y token:\"'") * 1_000 * s,
        "hidden_value_ladder": lambda s: _hidden_value_ladder(50 * s, 100_000 * s),
    },
    redact_secrets,
)
class HiddenNamesLinearTimeTest(unittest.TestCase):
    """Each value a hidden name could outlast is looked for only next to
    the end it outlasts, and a value an earlier one already reaches past
    is not read again."""


@_linear_time_tests(
    {
        "token_equals": lambda s: "token=" * 8_000 * s,
        "token_equals_quote": lambda s: "token='" * 8_000 * s,
        "token_equals_escaped_quote": lambda s: "token=\\'" * 8_000 * s,
        "token_equals_bytes_quote": lambda s: "token=b'" * 8_000 * s,
        "token_quoted_apostrophe": lambda s: "token='it's " * 4_000 * s,
        "bearer": lambda s: "Bearer " * 8_000 * s,
        "glued_bearer_plus": lambda s: "x_Bearer+" * 6_000 * s,
        "authorization_basic": lambda s: "Authorization: Basic " * 3_000 * s,
        "authorization_encoded_quoted_basic": lambda s: "Authorization: %22Basic%22 " * 3_000 * s,
        "authorization_bytes_encoded_basic": lambda s: "Authorization: b%22Basic%22 " * 3_000 * s,
        "bearer_double_encoded_space": lambda s: "Bearer%2520" * 5_000 * s,
        "userinfo_urls": lambda s: "a://a@" * 8_000 * s,
        "encoded_scheme_separator": lambda s: "x%3A%2F%2F" * 5_000 * s,
        "encoded_at_signs_in_netloc": lambda s: "a://" + "%40x" * 16_000 * s,
        "quotes": lambda s: "'" * 48_000 * s,
        "backslashes_then_quote": lambda s: "\\" * 48_000 * s + "'",
        "nested_escapes": _nested_escape,
        "broken_escapes": lambda s: "%2%34" * 10_000 * s,
        "percent_signs": lambda s: "%" * 48_000 * s,
        "encoded_values_with_deep_ampersands": lambda s: ("token%3Dx" + "%252526") * 3_000 * s,
        "encoded_values_then_ampersands": lambda s: (
            "token%3Dx" * 4_000 * s + "%2526" * 16_000 * s + "%26"
        ),
        "deep_name_then_values": lambda s: "token%25253D" + "x%2526" * 8_000 * s,
        "bypass_equals": lambda s: "bypass=" * 8_000 * s,
        "json_escaped_sig": lambda s: "\\u0026sig=" * 5_000 * s,
        "password_flags": lambda s: "--password " * 5_000 * s,
        "token_then_backslashes": lambda s: "token" + "\\" * 48_000 * s,
        "token_then_spaces": lambda s: "token" + " " * 48_000 * s,
        "authorization_then_punctuation": lambda s: "Authorization: " + "!" * 48_000 * s,
        "authorization_value_then_punctuation": lambda s: "Authorization: x" + "!" * 48_000 * s,
        "dashes_then_token": lambda s: "-" * 25_000 * s + "token x",
    }
)
class TokenizerShapesLinearTimeTest(unittest.TestCase):
    """Every value, credential and netloc is read from where it starts,
    including inside another one, so each ends at a stop looked up rather
    than scanned for: a scan from each start reads the same stretch once
    per value that starts in it. The escapes a first decoding assembles
    (`%253%34` is `%34` is `4`) are decoded in one pass however deep they
    nest, where a pass per level is quadratic in the nesting."""


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

    def test_the_last_at_sign_is_found_past_deeper_ones(self):
        """Percent-encoded `@`s after the netloc's last raw one sit deeper than
        the URL, so the lookup has to step over them to the raw one: the mask
        runs to it, and a `b` before it must not stay in view."""
        for line, want in (
            ("https://a:p@b@c%40d%40e/", "https://REDACTED@c%40d%40e/"),
            ("https://a@b@c%40d%40e%40f/z", "https://REDACTED@c%40d%40e%40f/z"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_a_long_run_of_scheme_characters_is_redacted_in_linear_time(self):
        """Every line `--log-file` and the console's log tail receive goes
        through here. A scheme pattern retried from every offset of a run of
        scheme characters is quadratic in the run: 64 KB of hex took 11 s, and
        the same run on a config line the parser refused took 17 s to quote."""
        for make in (
            lambda s: "a" * 16_000 * s,
            lambda s: "deadbeef0123" * 1_250 * s,
            lambda s: "x://" + "a" * 16_000 * s,
        ):
            with self.subTest(line=make(1)[:16]):
                line = make(_SCALE)
                self.assertEqual(redact_secrets(line), line)
                _assert_linear_time(self, make)


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
            'dma_password-"hunter2"',
            'dma_password-= "hunter2"',
            'dma_password_"hunter2"',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line(["[ultimate64]", line], 2)
                self.assertNotIn("hunter2", safe)
                self.assertIn("dma_password", safe)
                self.assertFalse(verbatim)

    def test_a_percent_encoded_name_on_a_rejected_line_still_cuts_it(self):
        """`redact_secrets` reads `%26sig%3D` decoded, as `&sig=`; read raw,
        the `6` glues onto `sig` and the signed URL came back verbatim."""
        for name in ("sig", "hmac", "key", "auth"):
            line = f'url = "https://h/a?x=1%26{name}%3Dhunter2" junk'
            with self.subTest(name=name):
                safe, verbatim = redact_source_line([line], 1)
                self.assertEqual(safe, line[: line.index("%3D")] + " REDACTED")
                self.assertFalse(verbatim)

    def test_an_encoded_dash_after_an_encoded_name_still_cuts_it(self):
        """The `-` read as a space has to be the decoded one as well: raw, the
        `%73` hides the name, and decoded the `-` glues onto it."""
        line = 'dma_pas%73word%2D"hunter2"'
        self.assertEqual(redact_source_line([line], 1), ("dma_pas%73word REDACTED", False))

    def test_a_dash_glued_exempt_word_on_a_rejected_line_is_kept(self):
        """A `-` is read as a space to find `dma_password-"x"`, but whether a
        name is one of the exempt words is still read as written."""
        line = 'filter = "high-pass" bogus'
        self.assertEqual(redact_source_line([line], 1), (line, True))

    def test_an_encoded_url_password_on_a_rejected_line_is_dropped(self):
        """`redact_secrets` reads userinfo decoded; read raw, an encoded `://`
        or `@` hid it and the line came back verbatim."""
        for line in (
            'url = "https%3A%2F%2Fkelly:hunter2%40host/a" junk',
            'url = "u64%3A//kelly:hunter2@host" junk',
        ):
            with self.subTest(line=line):
                safe, verbatim = redact_source_line([line], 1)
                self.assertEqual(safe, 'url = "REDACTED')
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


class NumberedNameTest(unittest.TestCase):
    def test_a_strong_secret_name_may_end_in_a_number(self):
        for line, want in (
            ("password2 = hunter2", "password2 = REDACTED"),
            ("Password12=hunter2 x", "Password12=REDACTED x"),
            ("token1=abc&x=1", "token1=REDACTED&x=1"),
            ('{"password2": "abc"}', '{"password2": "REDACTED"}'),
            ("x_secret3=abc", "x_secret3=REDACTED"),
            ("password_2=abc", "password_2=REDACTED"),
            ("api-key-7: abc", "api-key-7: REDACTED"),
            ("dbpwd2=abc", "dbpwd2=REDACTED"),
            ("PWD2=abc", "PWD2=REDACTED"),
            ("authorization2: Basic abc", "authorization2: Basic REDACTED"),
            ("%26password2%3Dabc%26n=1", "%26password2%3DREDACTED%26n=1"),
            ("--password2 hunter2 x", "--password2 REDACTED x"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)
        safe, verbatim = redact_source_line(['password2 == "hunter2"'], 1)
        self.assertNotIn("hunter2", safe)
        self.assertFalse(verbatim)

    def test_a_short_name_or_a_longer_word_keeps_its_value(self):
        line = (
            "key2=a sig2=b auth2=c hmac2=d bearer2=e password2x=f password2-x=g "
            "passes2=3 bypass2=on PWD=/x jwts2=1 token2_x=h"
        )
        self.assertEqual(redact_secrets(line), line)

    def test_a_long_run_of_digits_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "token" + "1" * 20_000 * s + "=x")
        _assert_linear_time(self, lambda s: "token1" * 8_000 * s)


class AuthorizationFlagTest(unittest.TestCase):
    def test_an_authorization_flag_keeps_its_scheme_and_masks_the_credential(self):
        for line, want in (
            ("--authorization Basic abc", "--authorization Basic REDACTED"),
            ("x --authorization Basic abc --verbose", "x --authorization Basic REDACTED --verbose"),
            ("x --authorization Basic abc y", "x --authorization Basic REDACTED y"),
            ("x --authorization\tBearer abc y", "x --authorization\tBearer REDACTED y"),
            ("x --proxy-authorization Digest abc", "x --proxy-authorization Digest REDACTED"),
            ('x --authorization "Basic abc" y', 'x --authorization "Basic REDACTED" y'),
            ("x -authorization Basic abc", "x -authorization Basic REDACTED"),
            ("x --authorization  Basic   abc d", "x --authorization  Basic   REDACTED d"),
            ("x --authorization (Basic) abc d", "x --authorization (Basic) REDACTED d"),
            ("x --authorization %22Basic%22 abc d", "x --authorization %22Basic%22 REDACTED d"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_authorization_flag_with_no_known_scheme_masks_both_words(self):
        self.assertEqual(redact_secrets("x --authorization s3cr3t y"), "x --authorization REDACTED")
        self.assertEqual(redact_secrets("x --authorization Basic"), "x --authorization REDACTED")

    def test_an_authorization_flag_with_no_value_is_left_alone(self):
        for line in ("x --authorization", "x --authorization --verbose"):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), line)

    def test_another_flag_still_takes_one_word(self):
        self.assertEqual(redact_secrets("x --password Basic abc"), "x --password REDACTED abc")

    def test_a_long_run_of_authorization_flags_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "--authorization Basic a " * 4_000 * s)
        _assert_linear_time(self, lambda s: "--authorization " * 8_000 * s)


class QuotedFlagTest(unittest.TestCase):
    def test_a_quoted_flag_masks_the_next_list_element(self):
        for line, want in (
            ("['--password', 'hunter2']", "['--password', 'REDACTED']"),
            ('["--password", "hunter2"]', '["--password", "REDACTED"]'),
            ("['--password','hunter2']", "['--password','REDACTED']"),
            ("['--password' , 'hunter2']", "['--password' , 'REDACTED']"),
            ("('--password', 'x')", "('--password', 'REDACTED')"),
            ("['--password', 'hunter2', '--verbose']", "['--password', 'REDACTED', '--verbose']"),
            ("['--password', 'a b c', 'd']", "['--password', 'REDACTED', 'd']"),
            ("['--password', b'hunter2']", "['--password', b'REDACTED']"),
            ("['--password', \"it's\"]", "['--password', \"REDACTED\"]"),
            ("'--password' 'hunter2' x", "'--password' 'REDACTED' x"),
            ("['--video-password', 'hunter2']", "['--video-password', 'REDACTED']"),
            ("['--stream-key', 's3']", "['--stream-key', 'REDACTED']"),
            ("['-token', 'abc']", "['-token', 'REDACTED']"),
            ("['--authorization', 'Basic abc']", "['--authorization', 'Basic REDACTED']"),
            ("%27--password%27%2C%20%27hunter2%27", "%27--password%27%2C%20%27REDACTED%27"),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_an_escaped_list_element_is_masked(self):
        for line in (
            '\\"--password\\", \\"hunter2\\" x',
            '"{\\"args\\": [\\"--password\\", \\"hunter2\\"]}"',
        ):
            with self.subTest(line=line):
                self.assertNotIn("hunter2", redact_secrets(line))

    def test_a_list_element_that_is_a_flag_or_a_lone_flag_is_not_a_value(self):
        for line in (
            "['--password', '--verbose']",
            "['--password', \"--verbose\"]",
            "['--password', b'-x']",
            "['--password-file', 'x']",
            "['--key', 'x']",
            "['--password']",
            "['--password', ]",
            '["--password","--x"]',
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), line)

    def test_prose_around_a_quoted_flag_keeps_its_words(self):
        line = 'the "--password", then enter it'
        self.assertEqual(redact_secrets(line), line)

    def test_a_flag_value_that_starts_with_a_letter_and_a_dash_is_still_a_value(self):
        self.assertEqual(redact_secrets("x --password b-x y"), "x --password REDACTED y")
        self.assertEqual(redact_secrets("x --password '-abc' y"), "x --password 'REDACTED' y")

    def test_a_long_run_of_quoted_flags_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "['--password', " * 6_000 * s)
        _assert_linear_time(self, lambda s: "'--password' " * 8_000 * s)
        _assert_linear_time(self, lambda s: "\\" * 20_000 * s + "--password' 'x")


class DigestParametersTest(unittest.TestCase):
    def test_every_digest_parameter_is_masked(self):
        for line, want in (
            (
                'Authorization: Digest username="u", response="abc123", cnonce="zz"',
                "Authorization: Digest REDACTED",
            ),
            ("Proxy-Authorization: Digest abc", "Proxy-Authorization: Digest REDACTED"),
            ("Authorization: Digest abc", "Authorization: Digest REDACTED"),
            (
                'Authorization: Digest username="u", uri="/x?a=1&b=2", response="abc" tail',
                "Authorization: Digest REDACTED",
            ),
            ("Authorization: digest a=b, c=\"d e\", f='g h'", "Authorization: digest REDACTED"),
            ('Authorization: Digest username="u", response="abc', "Authorization: Digest REDACTED"),
            (
                "Authorization: Digest username=u, response=abc&n=1",
                "Authorization: Digest REDACTED&n=1",
            ),
            (
                'x --authorization Digest username="u", response="abc"',
                "x --authorization Digest REDACTED",
            ),
            (
                "h=Authorization%3A%20Digest%20username%3D%22u%22%2C%20response%3D%22abc%22&x=1",
                "h=Authorization%3A%20Digest%20REDACTED&x=1",
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_the_value_ends_at_the_quote_that_closes_the_header(self):
        for line, want in (
            (
                "{'Authorization': 'Digest username=\"u\", response=\"abc\"', 'next': 'v'}",
                "{'Authorization': 'Digest REDACTED', 'next': 'v'}",
            ),
            (
                '{"Authorization": "Digest username=\\"u\\", response=\\"abc\\"", "next": "v"}',
                '{"Authorization": "Digest REDACTED", "next": "v"}',
            ),
            (
                '"Authorization: Digest username=\\"u\\", response=\\"abc\\"" next',
                '"Authorization: Digest REDACTED" next',
            ),
            (
                '\'Authorization: Digest username="u", response="abc"\' next',
                "'Authorization: Digest REDACTED' next",
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(redact_secrets(line), want)

    def test_another_scheme_still_ends_at_its_first_word(self):
        self.assertEqual(
            redact_secrets("Authorization: Basic abc, d=e"), "Authorization: Basic REDACTED, d=e"
        )

    def test_a_long_run_of_digest_headers_is_redacted_in_linear_time(self):
        for make in (
            lambda s: "Authorization: Digest a=b " * 4_000 * s,
            lambda s: 'Authorization: Digest a="' * 4_000 * s,
            lambda s: "Authorization: Digest " + 'a="b" ' * 8_000 * s + "'",
            lambda s: 'Authorization: Digest \\\\"' * 4_000 * s,
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


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
