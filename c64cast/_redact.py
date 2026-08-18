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

# Keyed on the `token=` suffix rather than on each parameter's full name, so
# `viewer_token=` and any later `…_token=` are covered by construction. The
# value stops at a query-string separator or quote, which leaves the rest of a
# login URL (`&next=/`) readable — the point is to keep the line diagnostic.
_TOKEN_VALUE = re.compile(r"(token=)[^\s&\"']+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """`text` with every ``token=VALUE`` reduced to ``token=REDACTED``."""
    return _TOKEN_VALUE.sub(rf"\g<1>{REDACTED}", text)
