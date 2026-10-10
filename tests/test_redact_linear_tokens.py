"""The tokenizer reads a run of names, credentials and netlocs in linear time.

Apart from `test_redact.py` so that unittest_parallel can run them on
other workers; the check itself is in `_redact_linear_time.py`."""

from __future__ import annotations

import unittest

from _redact_linear_time import _linear_time_tests


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
    }
)
class TokenizerShapesLinearTimeTest(unittest.TestCase):
    """Every value, credential and netloc is read from where it starts,
    including inside another one, so each ends at a stop looked up rather
    than scanned for: a scan from each start reads the same stretch once
    per value that starts in it. The escapes a first decoding assembles
    (`%253%34` is `%34` is `4`) are decoded in one pass however deep they
    nest, where a pass per level is quadratic in the nesting."""


if __name__ == "__main__":
    unittest.main()
