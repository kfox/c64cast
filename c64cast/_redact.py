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

import bisect
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
#: ``viewer_token`` and ``client_secret`` match. ``key``, ``sig``,
#: ``signature`` and ``hmac`` are too short for that: a prefix has to end in ``_`` or ``-``,
#: which is what makes the word its own component of the name rather than the
#: tail of another one. So ``?key=``, ``api_key=``, ``signing-key=``, ``?sig=``
#: ``X-Amz-Signature=`` and the ``hmac=`` inside an Akamai ``__token__=`` or
#: ``hdnts=`` match — the spellings signed media and feed URLs use — while ``sortkey=``, ``hotkey=``, ``monkey=``, ``sig_level=`` and
#: ``sigma=`` do not.
#:
#: The rule is positional and knows nothing about meaning, so a name whose last
#: component happens to be one of the four is masked whatever it holds:
#: ``cache_key=`` loses its value. That direction is the cheap one — a
#: diagnostic value goes missing from two destinations — and the reverse is a
#: credential in a file that outlives the run.
#:
#: The prefixed form is tried only at the first word character of a run of name
#: characters (`(?<![\w-]) -* \b`), not at every `\b`. A `-` puts a word
#: boundary at every letter of `a-a-a-…`, and each of those used to scan
#: `[\w-]*` to the end of the run — quadratic, so 10 KB of one took 1.5 s and
#: 100 KB took 139 s on a log line. A prefixed match from later in the run is
#: also one from that first character, since `[\w-]*` takes anything in
#: between, so the same name is found and it ends in the same place. The open
#: form is still tried at every `\b`, which costs one word each.
#:
#: It is also tried just after a percent-escape. A query value that is itself
#: URL-encoded spells `&sig=` as `%26sig%3D` and Akamai's `~hmac=` as
#: `%7Ehmac%3D`, and the escape's last hex digit is a word character, so no
#: `\b` falls before the name. That start costs one scan per escape, and an
#: escape ends the run before it, so the scan stays linear.
#:
#: A URL carried inside a parameter of a URL that is itself a parameter is
#: encoded twice, and there `&sig=` is `%2526sig%253D`; one level deeper it is
#: `%252526sig%25253D`. Each level puts one more `25` after every `%`, so the
#: escape before the name and the separator after it are read as `%`, any run
#: of `25`, then the escape's own digits. The run is taken possessively: given
#: back one pair at a time, every pair would rescan the name characters after
#: it, which is quadratic on a long run of `25`. A value encoded `n` times ends
#: at an `&` of any shallower level — `%26`, `%2526`, … up to `n - 1` pairs of
#: `25` — but not at one with `n` or more, which is a `%26` inside the secret
#: itself: a once-encoded value keeps its `%2526`.
#:
#: `%3A` counts as a separator too, and an encoded quote may close the name, as
#: their raw spellings do: a JSON document carried in a query parameter spells
#: `"token":"v"` as `%22token%22%3A%22v%22`. Such a value has no encoded quote
#: to end at, so it runs on to the `%26` or `&` and takes the closing quote and
#: whatever follows with it.
#:
#: The open form appears in both branches, spliced from one spelling so a name
#: added to it is found at a run's start and partway through it alike.
_OPEN_SECRET_NAME = r"\w* (?: token | password | secret | api[_-]?key )"
_SECRET_KEY = r"""
    (?:
        (?: (?<![\w-]) -* \b | % (?:25)*+ (?:[0-9a-f]{2})? (?<=[0-9a-f]) ) (?:
            {open}
          | (?: [\w-]* [_-] )? (?: key | sig (?:nature)? | hmac )
        )
      | \b {open}
    ) \b
""".replace("{open}", _OPEN_SECRET_NAME)

# Spliced by `.replace` rather than by an f-string: the pattern's own `{3}`
# repetition counts would have to be doubled to survive one.
_SECRET_VALUE = re.compile(
    r"""
    (?P<kv_prefix>
        {key} (?: ["'] | %(?:25)*+2[27] )? \s* (?: [=:] | (?P<pct> % (?P<enc> (?:25)*+ ) 3[ad] ) ) \s*
        (?P<quote> ["]{3} | [']{3} | ["'] )?
    )
    (?P<kv_value>
        (?(quote) (?: \\[^\r\n] | (?!(?P=quote)) [^\r\n] )+
        | (?(pct) (?: (?! (?!%(?P=enc)25) %(?:25)*+26 ) [^\s&"',}] )+ | [^\s&"',}]+ ) )
    )
    """.replace("{key}", _SECRET_KEY),
    re.IGNORECASE | re.VERBOSE,
)

#: `Bearer VALUE`, searched for on its own rather than as a third branch of
#: :data:`_SECRET_VALUE`. One pattern's matches cannot overlap, so there an
#: unquoted value ending at the space after `Bearer` took the word as the whole
#: secret and hid the token behind it: `access_token: Bearer eyJ…` kept the
#: `eyJ…`. The value is read inside a lookahead for the same reason, so the
#: next search starts where it does: in `Bearer Bearer eyJ…` the second
#: `Bearer` is the first one's value and the start of a match of its own. A
#: value holds no whitespace and a match needs some after its `Bearer`, so no
#: character is read as a value twice.
#:
#: `Bearer` is also tried right after a percent-escape, as a name is: in
#: `%22token%22%3ABearer abc` the escape's hex digit leaves no `\b` before it,
#: and the `token` value ends at the space, so neither pattern reached `abc`.
#: An encoded header spells the space `%20` or `+` (`Authorization%3A%20Bearer%20abc`).
#: A value after an encoded space ends at the next one, which keeps the rule
#: above: a value never holds the kind of space its `Bearer` was matched by.
_BEARER_VALUE = re.compile(
    r"""
    (?: \b | % (?:25)*+ (?:[0-9a-f]{2})? (?<=[0-9a-f]) ) Bearer
    (?: \s+ | (?P<enc> (?: \s | % (?:25)*+ 20 | \+ )+ ) )
    (?= (?P<value> (?(enc) (?: (?! % (?:25)*+ 20 ) [^\s"',}+] )+ | [^\s"',}]+ ) ) )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SECRET_KEY_RE = re.compile(_SECRET_KEY, re.IGNORECASE | re.VERBOSE)

_TRIPLE = ('"""', "'''")

#: `scheme://` followed by anything up to an `@` that is still inside the
#: netloc. Deliberately not `urlsplit`: a line the TOML parser rejected may
#: hold no parseable URL at all, and the point is to spot the *shape* of
#: userinfo without needing the line to be well formed. The netloc ends at the
#: first `/`, `?` or `#`, so those bound the search and an `@` later in a path
#: or query is not userinfo.
#:
#: A quote does not bound it, of either kind. RFC 3986 allows a `'` unencoded
#: in userinfo, and `file = 'https://kelly:it's@cdn/a.mp4'` is a literal string
#: that the `'` ends early, so the parser rejects exactly that line and it
#: comes here. A `"` is the same shape in a basic string —
#: `file = "https://kelly:it"s@cdn/a.mp4"`, or `it\"s` escaped on a line
#: refused for something else — and a password does not have to be a legal URL
#: to be a password. The cost is `x = ["tr://COM3", "me@host"]` cut at the
#: scheme on a line the parser refused for some other reason.
#:
#: Whitespace deliberately does *not* bound it. A space is illegal in a URL, so
#: reading one as the end of the netloc is defensible — but a passphrase with a
#: space in it is precisely the malformed shape this runs on, and
#: `u64://kelly:my pass@host` would otherwise come back whole.
#:
#: Both userinfo patterns are anchored on the literal `://` and only *look
#: behind* it for a scheme character, rather than matching the scheme itself.
#: A `[a-z][a-z0-9+.\-]*://` pattern is retried from every offset of a run of
#: scheme characters and scans the rest of the run each time, which is
#: quadratic: a 64 KB hex dump on one log line took 11 s to redact, and every
#: line `--log-file` or the console's log buffer receives goes through it.
#: :func:`_scheme_start` recovers where the scheme began when a caller needs it.
_URL_USERINFO = re.compile(r"://(?<=[a-z0-9+.\-]://)[^/?#]*@", re.IGNORECASE)

#: The characters a URL scheme is spelled with (RFC 3986 §3.1).
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")


#: Userinfo inside a well-formed URL on a log line: `scheme://user:pass@`. Unlike
#: `_URL_USERINFO` (for malformed config lines) whitespace *does* bound it here:
#: a URL a program opened has no raw space in it, and an unbounded match would
#: run from `tr://COM3` across a whole sentence to someone's `me@example.com`.
#: Greedy up to the netloc's last `@`, so a password holding a raw `@` goes too.
#: A quote does not bound it: RFC 3986 allows a `'` unencoded in userinfo, so
#: `https://user:it's@host` is well formed and its password has to go too, and
#: `file = "https://user:it\"s@host/a.mp4"` parses to a URL FFmpeg quotes back
#: with a raw `"` in the password.
_INLINE_URL_USERINFO = re.compile(r"://(?<=[a-z0-9+.\-]://)[^\s/?#]*@", re.IGNORECASE)


def _scheme_start(line: str, separator: int) -> int:
    """The index in `line` where the scheme ending at the `://` found at
    `separator` begins."""
    i = separator
    while i > 0 and line[i - 1] in _SCHEME_CHARS:
        i -= 1
    return i


Span = tuple[int, int]


def _value_spans(text: str) -> list[Span]:
    """Where in `text` the name rule finds a secret value: each value a
    :data:`_SECRET_VALUE` key names, and each token :data:`_BEARER_VALUE`
    finds. The two may overlap."""
    return [m.span("kv_value") for m in _SECRET_VALUE.finditer(text)] + [
        m.span("value") for m in _BEARER_VALUE.finditer(text)
    ]


def _splice(text: str, spans: Sequence[Span]) -> str:
    """`text` with each of `spans` — sorted, none overlapping — replaced by
    ``REDACTED``. An empty span still gets one: `://@` is userinfo too."""
    out: list[str] = []
    done = 0
    for start, end in spans:
        out += (text[done:start], REDACTED)
        done = end
    out.append(text[done:])
    return "".join(out)


def _merge(spans: list[Span]) -> list[Span]:
    """`spans` sorted, with every pair that overlaps or touches made one."""
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _spliced_starts(userinfo: Sequence[Span]) -> list[int]:
    """Where each of `userinfo`'s ``REDACTED`` begins once :func:`_splice` has
    put it in."""
    out_starts: list[int] = []
    shift = 0
    for start, end in userinfo:
        out_starts.append(start + shift)
        shift += len(REDACTED) - (end - start)
    return out_starts


def _source_span(span: Span, userinfo: Sequence[Span], out_starts: Sequence[int]) -> Span:
    """`span`, found in the text `userinfo` was spliced out of, as a span of
    the text before the splice; `out_starts` is :func:`_spliced_starts` of
    `userinfo`. An end inside a ``REDACTED`` widens to the whole of the
    userinfo it replaced."""

    def back(p: int, *, is_end: bool) -> int:
        i = (bisect.bisect_left if is_end else bisect.bisect_right)(out_starts, p) - 1
        if i < 0:
            return p
        start, end = userinfo[i]
        inside_end = out_starts[i] + len(REDACTED)
        if (p <= inside_end) if is_end else (p < inside_end):
            return end if is_end else start
        return p - inside_end + end

    return back(span[0], is_end=False), back(span[1], is_end=True)


def redact_secrets(text: str) -> str:
    """`text` with every recognized secret value reduced to ``REDACTED`` —
    `token=VALUE`, `password: VALUE`, `secret=VALUE`, `api_key=VALUE`,
    `key=VALUE`, `sig=VALUE`, `signature=VALUE`, `hmac=VALUE` (`=` or `:`, and the first four
    with any prefix, so `viewer_token` and `client_secret` match),
    `Bearer VALUE`, and the userinfo of a URL (`https://user:pass@host` comes
    back as `https://REDACTED@host`) — a private media file is legitimately
    reached that way, and FFmpeg quotes the URL it failed on into its errors.

    The short names take a prefix only when a `_` or `-` separates it, so
    `signing_key=` is covered and `sortkey=` is left alone.

    An unquoted value ends at whitespace, `&`, a comma, a quote, or a closing
    brace. A name inside a URL-encoded value (`%26sig%3DVALUE`,
    `%7Ehmac%3DVALUE`) is matched too, and its value also ends at `%26`; so
    is one encoded twice (`%2526sig%253DVALUE`), whose value also ends at
    `%2526` or `%26`, and so on for each further level. `%3A` and an encoded quote are read as their raw
    spellings are, so `%22token%22%3AVALUE` is covered. A
    quoted one — `'`, `"`, `'''` or `\"\"\"` — runs to the matching
    quote that no backslash escapes, or to the end of the line, whichever comes
    first: a value written across several lines is masked only as far as its
    first newline, and one whose opening delimiter ends the line has nothing on
    that line to mask.

    Masking a value means finding where it starts and ends, which a malformed
    line does not offer — :func:`redact_source_line` is for the caller quoting
    one of those.

    The userinfo rule and the name rule are each applied to `text` as given,
    and a character either one would mask is masked. Running one over the
    other's output leaks: no quote bounds the userinfo, so on a whitespace-free
    run such as `{"url":"u64://host","dma_password":"hunt@er2"}` it reaches
    from one string into the next, swallows the key name, and the `er2` past
    its `@` was left with nothing to name it a secret. The name rule is also
    run over the userinfo-masked text, since a quote inside the userinfo can
    end a value early that the masked text lets run on."""
    userinfo = [(m.start() + 3, m.end() - 1) for m in _INLINE_URL_USERINFO.finditer(text)]
    spans = userinfo + _value_spans(text)
    if userinfo:
        masked, out_starts = _splice(text, userinfo), _spliced_starts(userinfo)
        spans += [_source_span(span, userinfo, out_starts) for span in _value_spans(masked)]
    return _splice(text, _merge(spans))


def _skip_quoted(line: str, start: int) -> int:
    """The index just past the single-line string opening at `start`, or the
    end of the line when nothing closes it. A basic string escapes its own
    delimiter with `\\`; a literal string has no escapes."""
    quote = line[start]
    i = start + 1
    while i < len(line):
        if quote == '"' and line[i] == "\\":
            i += 2
        elif line[i] == quote:
            return i + 1
        else:
            i += 1
    return len(line)


def _value_open(lines: Sequence[str], lineno: int) -> bool:
    """Whether a value opened earlier is still open when the 1-based
    `lineno`-th line is reached — a `'''` or `\"\"\"` string, an array, or an
    inline table.

    Only the lines *before* `lineno` are read, and the parser consumed those
    before it rejected this one, so they are well formed enough to walk: a
    comment runs to the end of its line, and a single-line string is stepped
    over whole. Counting every `\"\"\"` run instead — including the ones a
    comment or a literal string merely mentions — flips the parity, and an
    opening delimiter read as a closing one hands back the continuation line
    that carries the passphrase."""
    delim: str | None = None
    depth = 0
    for line in lines[: lineno - 1]:
        i = 0
        while i < len(line):
            if delim is not None:
                if line.startswith(delim, i):
                    delim, i = None, i + 3
                elif delim == '"""' and line[i] == "\\":
                    i += 2
                else:
                    i += 1
            elif line[i] == "#":
                break
            elif line.startswith(_TRIPLE, i):
                delim, i = line[i : i + 3], i + 3
            elif line[i] in "\"'":
                i = _skip_quoted(line, i)
            else:
                if line[i] in "[{":
                    depth += 1
                elif line[i] in "]}":
                    # Floored rather than allowed to go negative: a closer the
                    # walk cannot account for would otherwise cancel a later
                    # genuine opener, and an open value read as closed is the
                    # direction that echoes the continuation line.
                    depth = max(depth - 1, 0)
                i += 1
    return delim is not None or depth > 0


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

    Three rules replace that test, and all of them hold whatever the line is
    malformed into:

    * A line naming a secret-shaped key keeps the name and loses everything
      after it. The name is the whole diagnostic — it says which setting the
      parser choked on — and no source text survives past it to be read.
      Everything, that is, unless the userinfo rule below cuts earlier.
    * A line reached while a value is still open is dropped whole, since it may
      be that value. Applied to any open value — a `'''` or `\"\"\"` string, an
      array, an inline table — and secret-shaped or not: which key a
      continuation line belongs to cannot be answered without parsing, and
      parsing is what failed.
    * A line carrying URL userinfo is truncated at the scheme. The key is the
      one place a secret can sit under a name that is not secret-shaped:
      `url = "u64://kelly:hunter2@host"` names `url`, so neither rule above
      fires and the password was echoed whole with a caret under it.
      :func:`c64cast.app.connect.redact_target` already collapses userinfo, but
      only once the target has *parsed* — and this function exists for the line
      that did not. It wins over the key rule when its cut is the earlier one,
      because the text that rule keeps is otherwise free to carry the
      credential through: `url = "https://kelly:hunter2@h/?api_key=x"` names
      `api_key` well past the password.

    `verbatim` is False whenever any of them fired, so a caller drawing a caret
    under a column drops it — the columns no longer point where they did."""
    if _value_open(lines, lineno):
        return REDACTED, False
    line = lines[lineno - 1]
    key = _SECRET_KEY_RE.search(line)
    userinfo = _URL_USERINFO.search(line)
    cut = _scheme_start(line, userinfo.start()) if userinfo is not None else None
    if cut is not None and (key is None or cut < key.end()):
        return f"{line[:cut]}{REDACTED}", False
    if key is not None:
        return f"{line[: key.end()]} {REDACTED}", False
    return line, True
