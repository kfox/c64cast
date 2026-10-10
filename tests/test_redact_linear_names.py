"""The redactor reads long runs of names, flags and hidden values in linear time.

Apart from `test_redact.py` so that unittest_parallel can run them on
other workers; the check itself is in `_redact_linear_time.py`."""

from __future__ import annotations

import unittest

from _redact_linear_time import _assert_linear_time, _hidden_value_ladder, _linear_time_tests

from c64cast._redact import redact_secrets


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


class RedactSecretsLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_encoded_bearers_is_redacted_in_linear_time(self):
        """Every other `Bearer` in the run starts a match, and the one between
        is its value (the `%20` a match consumes leaves that one no escape to
        follow). A value that ran past an encoded space read the rest of the run
        once per match, which is quadratic: 144 KB of `Bearer%20` took 3.6 s."""
        _assert_linear_time(self, lambda s: "Bearer%20" * 4_000 * s, redact_secrets)

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


class QuotedFlagLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_quoted_flags_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "['--password', " * 6_000 * s)
        _assert_linear_time(self, lambda s: "'--password' " * 8_000 * s)
        _assert_linear_time(self, lambda s: "\\" * 20_000 * s + "--password' 'x")
        _assert_linear_time(self, lambda s: "('password', " * 6_000 * s)
        _assert_linear_time(self, lambda s: "('password', 'a'" * 4_000 * s)
        _assert_linear_time(self, lambda s: "('Set-Cookie', '" * 4_000 * s)
        _assert_linear_time(self, lambda s: "(\\'" * 20_000 * s + "password\\', \\'x")


class NumberedNameLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_digits_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "token" + "1" * 20_000 * s + "=x")
        _assert_linear_time(self, lambda s: "token1" * 8_000 * s)


class AuthorizationFlagLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_authorization_flags_is_redacted_in_linear_time(self):
        _assert_linear_time(self, lambda s: "--authorization Basic a " * 4_000 * s)
        _assert_linear_time(self, lambda s: "--authorization " * 8_000 * s)


if __name__ == "__main__":
    unittest.main()
