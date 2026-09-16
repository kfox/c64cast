"""Secret redaction for the log destinations the operator does not solely read.

The web console's token is logged as a ready-to-open login URL because that URL
is the only entry point a phone gets — so it has to reach the *terminal*
intact. Two other destinations carry the same line and should not:

* ``--log-file`` writes a plain file that outlives the run and is not created
  ``0600``, while the token's own store deliberately is. A token that leaks
  there stays valid, because the host keeps it across restarts.
* the console's own :class:`~c64cast.app.serve.SessionLogBuffer` is served over
  the state feed to *every* client, including a read-only viewer — whose entire
  point is that it cannot control the show. Handing one the admin token in a
  log tail turns a viewer link into a full credential.

So both of those redact and the terminal does not. Redacting at the point of
*output* rather than at the ``log.info`` call is what makes that split
possible.
"""

from __future__ import annotations

import re

#: What a redacted value is replaced with. Deliberately not the same length as
#: any real token — a fixed-width mask invites the reader to guess.
REDACTED = "REDACTED"

_SECRET_VALUE = re.compile(
    r"""
    (?P<kv_prefix>
        \b\w*(?:token|password|secret|api[_-]?key)\b ["']? \s* [=:] \s*
        (?P<quote> ["]{3} | [']{3} | ["'] )?
    )
    (?P<kv_value>
        (?(quote) (?: \\[^\r\n] | (?!(?P=quote)) [^\r\n] )+ | [^\s&"',}]+ )
    )
    |
    (?P<bearer_prefix>\bBearer\s+) (?P<bearer_value>[^\s"',}]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _mask(m: re.Match[str]) -> str:
    prefix = m.group("kv_prefix")
    return f"{prefix if prefix is not None else m.group('bearer_prefix')}{REDACTED}"


def redact_secrets(text: str) -> str:
    """`text` with every recognized secret value reduced to ``REDACTED`` —
    `token=VALUE`, `password: VALUE`, `secret=VALUE`, `api_key=VALUE` (`=` or
    `:`, with any prefix, so `viewer_token` and `client_secret` match) and
    `Bearer VALUE`.

    An unquoted value ends at whitespace, `&`, a comma, a quote, or a closing
    brace. A quoted one — `'`, `"`, `'''` or `\"\"\"` — runs to the matching
    quote that no backslash escapes, or to the end of the line, whichever comes
    first: a value written across several lines is masked only as far as its
    first newline, and one whose opening delimiter ends the line has nothing on
    that line to mask."""
    return _SECRET_VALUE.sub(_mask, text)
