"""The redactor reads a long run of cookie headers in linear time.

Apart from `test_redact.py` so that unittest_parallel can run them on
other workers; the check itself is in `_redact_linear_time.py`."""

from __future__ import annotations

import unittest

from _redact_linear_time import _linear_time_tests


@_linear_time_tests(
    {
        "cookie_equals": lambda s: "cookie=" * 8_000 * s,
        "cookie_colon_space": lambda s: "Cookie: " * 8_000 * s,
        "cookie_pairs": lambda s: "Cookie:" + "a=b;" * 8_000 * s,
        "set_cookie_pair": lambda s: "Set-Cookie: a=b;" * 4_000 * s,
        "cookie_equals_quote": lambda s: "cookie='" * 8_000 * s,
        "cookie_equals_encoded_quote": lambda s: "cookie=%22" * 4_000 * s,
        "cookie_quoted_name": lambda s: 'cookie: "a="' * 4_000 * s,
        "cookie_value_then_quoted_cookie": lambda s: "x cookie=a;cookie='b;" * 4_000 * s,
        "cookie_flag_list": lambda s: "'--cookie', 'a=b'," * 4_000 * s,
        "cookie_quoted_value": lambda s: 'cookie: "a"; ' * 4_000 * s,
        "quoted_cookie_header": lambda s: "'Cookie: a=b' " * 4_000 * s,
        "quoted_cookie_then_spaces": lambda s: "'Cookie: '" + " " * 40_000 * s + "x",
        "cookie_spaced_names": lambda s: "cookie=" + "a b=" * 8_000 * s,
        "cookie_empty_quotes": lambda s: "cookie: '' " * 4_000 * s,
        "cookie_empty_encoded_quotes": lambda s: "cookie=%22%22 " * 4_000 * s,
        "cookie_empty_quotes_then_spaces": lambda s: "Cookie: ''" + " " * 40_000 * s + "x",
        "set_cookie_commas": lambda s: "Set-Cookie: " + ",a=" * 8_000 * s,
        "set_cookie_comma_spaces": lambda s: "Set-Cookie: " + ",    " * 8_000 * s,
        "set_cookie_paths": lambda s: "Set-Cookie: a=1" + "; Path=/" * 8_000 * s,
        "encoded_cookie_pairs": lambda s: "Cookie%3A%20" + "a%3Dx%26" * 4_000 * s,
        "set_cookie_encoded_apostrophe": lambda s: "Set-Cookie: %27&" * 500 * s,
        "cookie_flag_encoded_apostrophe": lambda s: "=['--cookie', %27" * 500 * s,
        "set_cookie_double_encoded_escape": lambda s: "://Set-Cookie: \\\\%2527  " * 400 * s,
    }
)
class CookieLinearTimeTest(unittest.TestCase):
    """A long run of cookie headers is redacted in linear time."""


if __name__ == "__main__":
    unittest.main()
