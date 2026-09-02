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

# Keyed on a `*token=`/`*password=`/`*secret=`/`*api[_-]key=` suffix rather
# than on each parameter's full name, so `viewer_token=`, `token:` (JSON) and
# any later `…_token=` are covered by construction rather than by naming each
# one here — plus an `Authorization: Bearer …` header value, the other shape
# the console's admin token can appear in. The value stops at whitespace, a
# query-string separator, or a quote/brace, which leaves the rest of a login
# URL (`&next=/`) or a JSON document's other fields readable — the point is
# to keep the line diagnostic, not to blank the whole thing.
_SECRET_VALUE = re.compile(
    r"""
    (?P<kv_prefix> \b\w*(?:token|password|secret|api[_-]?key)\b "? \s* [=:] \s* "? )
    (?P<kv_value>[^\s&"',}]+)
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
    `token=VALUE`, `password: VALUE`, `api_key=VALUE` (`=` or `:`, quoted or
    not) and `Bearer VALUE`."""
    return _SECRET_VALUE.sub(_mask, text)
