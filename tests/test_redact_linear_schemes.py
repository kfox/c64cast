"""The redactor reads long runs of schemes, quotes and escapes in linear time.

Apart from `test_redact.py` so that unittest_parallel can run them on
other workers; the check itself is in `_redact_linear_time.py`."""

from __future__ import annotations

import unittest

from _redact_linear_time import _SCALE, _assert_linear_time

from c64cast._redact import redact_secrets


class PunctuatedSchemeLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_punctuated_words_is_redacted_in_linear_time(self):
        for make in (
            lambda s: "Authorization:" * 8_000 * s + "x" + " " * 20_000 * s + "y z",
            lambda s: "Authorization: s3!x " * 4_000 * s,
            lambda s: "Authorization: " + "s3!x" * 20_000 * s + " a",
            lambda s: "Authorization:s3!x" * 8_000 * s + " " * 20_000 * s + "a",
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


class QuotedSchemeLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_quoted_schemes_is_redacted_in_linear_time(self):
        for make in (
            lambda s: 'Authorization: "Basic" ' * 4_000 * s,
            lambda s: 'Authorization:"' * 8_000 * s + 'Basic"' + " " * 20_000 * s + "a",
            lambda s: 'Authorization: "Digest" ' * 4_000 * s,
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


class RedactUrlUserinfoLinearTimeTest(unittest.TestCase):
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


class DigestParametersLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_digest_headers_is_redacted_in_linear_time(self):
        for make in (
            lambda s: "Authorization: Digest a=b " * 4_000 * s,
            lambda s: 'Authorization: Digest a="' * 4_000 * s,
            lambda s: "Authorization: Digest " + 'a="b" ' * 8_000 * s + "'",
            lambda s: 'Authorization: Digest \\\\"' * 4_000 * s,
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


class WordBeforeQuoteLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_words_before_quotes_is_redacted_in_linear_time(self):
        for make in (
            lambda s: "token=Qz'" * 8_000 * s,
            lambda s: "token=Qz'a " * 4_000 * s,
            lambda s: "token=" + "a" * 20_000 * s + "'",
            lambda s: "Bearer " + "Qz'a b' " * 4_000 * s,
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


class EscapeBeforeNameLinearTimeTest(unittest.TestCase):
    def test_a_long_run_of_escapes_is_redacted_in_linear_time(self):
        e = "\\"
        for make in (
            lambda s: (e + "nsig=a ") * 4_000 * s,
            lambda s: (e + "u0026") * 8_000 * s + "--password a",
            lambda s: "x" + (e + "n") * 8_000 * s + "Cookie: a=b",
            lambda s: (e + "n--password a ") * 4_000 * s,
        ):
            with self.subTest(line=make(1)[:30]):
                _assert_linear_time(self, make)


if __name__ == "__main__":
    unittest.main()
