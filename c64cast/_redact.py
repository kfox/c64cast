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
from collections.abc import Sequence

#: What a redacted value is replaced with. Deliberately not the same length as
#: any real token — a fixed-width mask invites the reader to guess.
REDACTED = "REDACTED"

#: The key names whose value is a secret, as a ``re.VERBOSE`` fragment shared
#: by the value pattern and :func:`redact_source_line` — one spelling, so a
#: name one of them recognizes the other does too.
#:
#: ``token``, ``password`` and ``secret`` take any prefix, glued or not, so
#: ``viewer_token`` and ``client_secret`` match. ``key``, ``sig`` and
#: ``signature`` are too short for that: a prefix has to end in ``_`` or ``-``,
#: which is what makes the word its own component of the name rather than the
#: tail of another one. So ``?key=``, ``api_key=``, ``signing-key=``, ``?sig=``
#: and ``X-Amz-Signature=`` match — the spellings signed media and feed URLs
#: use — while ``sortkey=``, ``hotkey=``, ``monkey=``, ``sig_level=`` and
#: ``sigma=`` do not.
#:
#: The rule is positional and knows nothing about meaning, so a name whose last
#: component happens to be one of the three is masked whatever it holds:
#: ``cache_key=`` loses its value. That direction is the cheap one — a
#: diagnostic value goes missing from two destinations — and the reverse is a
#: credential in a file that outlives the run.
_SECRET_KEY = r"""
    \b (?:
        \w* (?: token | password | secret | api[_-]?key )
      | (?: [\w-]* [_-] )? (?: key | sig (?:nature)? )
    ) \b
"""

# Spliced by `.replace` rather than by an f-string: the pattern's own `{3}`
# repetition counts would have to be doubled to survive one.
_SECRET_VALUE = re.compile(
    r"""
    (?P<kv_prefix>
        {key} ["']? \s* [=:] \s*
        (?P<quote> ["]{3} | [']{3} | ["'] )?
    )
    (?P<kv_value>
        (?(quote) (?: \\[^\r\n] | (?!(?P=quote)) [^\r\n] )+ | [^\s&"',}]+ )
    )
    |
    (?P<bearer_prefix>\bBearer\s+) (?P<bearer_value>[^\s"',}]+)
    """.replace("{key}", _SECRET_KEY),
    re.IGNORECASE | re.VERBOSE,
)

_SECRET_KEY_RE = re.compile(_SECRET_KEY, re.IGNORECASE | re.VERBOSE)

_TRIPLE_QUOTE = re.compile("\"\"\"|'''")


def _mask(m: re.Match[str]) -> str:
    prefix = m.group("kv_prefix")
    return f"{prefix if prefix is not None else m.group('bearer_prefix')}{REDACTED}"


def redact_secrets(text: str) -> str:
    """`text` with every recognized secret value reduced to ``REDACTED`` —
    `token=VALUE`, `password: VALUE`, `secret=VALUE`, `api_key=VALUE`,
    `key=VALUE`, `sig=VALUE`, `signature=VALUE` (`=` or `:`, and the first four
    with any prefix, so `viewer_token` and `client_secret` match) and
    `Bearer VALUE`.

    The short names take a prefix only when a `_` or `-` separates it, so
    `signing_key=` is covered and `sortkey=` is left alone.

    An unquoted value ends at whitespace, `&`, a comma, a quote, or a closing
    brace. A quoted one — `'`, `"`, `'''` or `\"\"\"` — runs to the matching
    quote that no backslash escapes, or to the end of the line, whichever comes
    first: a value written across several lines is masked only as far as its
    first newline, and one whose opening delimiter ends the line has nothing on
    that line to mask.

    Masking a value means finding where it starts and ends, which a malformed
    line does not offer — :func:`redact_source_line` is for the caller quoting
    one of those."""
    return _SECRET_VALUE.sub(_mask, text)


def _multiline_open(lines: Sequence[str], lineno: int) -> bool:
    """Whether a `'''` or `\"\"\"` value opened earlier is still open when the
    1-based `lineno`-th line is reached."""
    delim: str | None = None
    for line in lines[: lineno - 1]:
        for m in _TRIPLE_QUOTE.finditer(line):
            if delim is None:
                delim = m.group()
            elif m.group() == delim:
                delim = None
    return delim is not None


def redact_source_line(lines: Sequence[str], lineno: int) -> tuple[str, bool]:
    """The 1-based `lineno`-th of `lines` in a form safe to quote back, and
    whether what comes back is that line verbatim.

    For a well-formed line :func:`redact_secrets` is enough. This is for the
    caller quoting a line a *parser rejected*, where the value has no bounds to
    find: `dma_password == "hunter2"` masks the doubled `=` and leaves the
    passphrase whole, `dma_password "hunter2"` offers nothing to anchor on, and
    a line inside a multi-line value carries no key name at all. In every one
    of those the substitution either lands on the wrong text or does not
    happen — so "the text changed" is not evidence that the secret is gone.

    Two rules replace that test, and both hold whatever the line is malformed
    into:

    * A line naming a secret-shaped key keeps the name and loses everything
      after it. The name is the whole diagnostic — it says which setting the
      parser choked on — and no source text survives past it to be read.
    * A line reached while a multi-line value is open is dropped whole, since
      it may be that value. Applied to any open value, secret-shaped or not:
      which key a continuation line belongs to cannot be answered without
      parsing, and parsing is what failed.

    `verbatim` is False whenever either fired, so a caller drawing a caret
    under a column drops it — the columns no longer point where they did."""
    if _multiline_open(lines, lineno):
        return REDACTED, False
    line = lines[lineno - 1]
    m = _SECRET_KEY_RE.search(line)
    if m is None:
        return line, True
    return f"{line[: m.end()]} {REDACTED}", False
