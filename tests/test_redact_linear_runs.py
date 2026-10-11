"""The tokenizer reads runs of quotes, escapes and punctuation in linear time.

Apart from `test_redact.py` so that unittest_parallel can run them on
other workers; the check itself is in `_redact_linear_time.py`."""

from __future__ import annotations

import unittest

from _redact_linear_time import _linear_time_tests, _nested_escape


@_linear_time_tests(
    {
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
class TokenizerRunsLinearTimeTest(unittest.TestCase):
    """Every value, credential and netloc is read from where it starts,
    including inside another one, so each ends at a stop looked up rather
    than scanned for: a scan from each start reads the same stretch once
    per value that starts in it. The escapes a first decoding assembles
    (`%253%34` is `%34` is `4`) are decoded in one pass however deep they
    nest, where a pass per level is quadratic in the nesting."""


if __name__ == "__main__":
    unittest.main()
