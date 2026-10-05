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
#: character is read as a value twice. That holds after a raw space or `+`, not
#: after an encoded one: the match consumes the `%20`, and its hex digit is what
#: a `Bearer` with no `\b` before it has to follow, so in `Bearer%20Bearer%20abc`
#: the second `Bearer` starts no match and `abc` is kept. In a run of them only
#: every other `Bearer` starts one.
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

#: :data:`_SECRET_VALUE` with `Bearer VALUE` back as a branch of its own, as it
#: was before the two were searched apart: a Bearer value is stepped over whole,
#: so no name starts inside it. Apart, a name can, and its quoted value can then
#: close inside a later name's quoted value and hide that name: in
#: `Bearer token=' x secret="a'bc"` the `token` value ends at the `'` inside
#: `"a'bc"` and kept `bc`. What this finds is added to the other spans, never
#: used in place of them, so it can only mask more.
_SECRET_VALUE_PAST_BEARER = re.compile(
    _SECRET_VALUE.pattern + r"""| \bBearer\s+[^\s"',}]+""",
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


#: A separator as :data:`_SECRET_VALUE` reads one. `enc` is the run of `25`
#: giving an encoded one's depth, and is None for a raw `=` or `:`.
_SEPARATOR = re.compile(r"[=:] | % (?P<enc> (?:25)*+ ) 3[ad]", re.IGNORECASE | re.VERBOSE)

#: An `&` at some depth of encoding — `%26`, `%2526`, … — which ends an
#: encoded value whose separator is at least that deep.
_ENCODED_AMP = re.compile(r"% (?P<enc> (?:25)*+ ) 26", re.IGNORECASE | re.VERBOSE)

#: What may end an unquoted value: a character that ends every one, or an
#: encoded `&`, which ends one only as :data:`_ENCODED_AMP` says.
_UNQUOTED_STOP = re.compile(r"""[\s&"',}] | % (?P<enc> (?:25)*+ ) 26""", re.VERBOSE)

#: The depth :func:`_hidden_values` gives a raw separator, which no encoded
#: `&` ends, and the rank it gives a value that ended where every value does.
_RAW, _EVERY = -1, -2


def _depth(m: re.Match[str]) -> int:
    enc = m.group("enc")
    return _RAW if enc is None else len(enc) // 2


def _is_name_char(c: str, *, dash: bool) -> bool:
    return c.isalnum() or c == "_" or (dash and c == "-")


def _back_over(text: str, lo: int, j: int, raw: str, hexes: tuple[str, ...]) -> int | None:
    """Where a character of `raw`, or a percent-escape of one of `hexes` at
    any depth, that ends at `j` begins — no earlier than `lo`; None if none
    ends there."""
    if j > lo and text[j - 1] in raw:
        return j - 1
    if j - 3 < lo or text[j - 2 : j].lower() not in hexes:
        return None
    i = j - 2
    while i > lo:
        if text[i - 1] == "%":
            return i - 1
        if i - 2 < lo or text[i - 2 : i] != "25":
            break
        i -= 2
    return None


def _back_over_space(text: str, lo: int, j: int) -> int:
    while j > lo and text[j - 1].isspace():
        j -= 1
    return j


def _name_starts(text: str, lo: int, k: int) -> list[int]:
    """Where a name ending at `k` may begin, no earlier than `lo`: its run of
    name characters, the percent-escape just before that run, and the last
    word of the run, which is where an open name past a `-` starts."""
    word = k
    while word > lo and _is_name_char(text[word - 1], dash=False):
        word -= 1
    run = word
    while run > lo and _is_name_char(text[run - 1], dash=True):
        run -= 1
    starts = [run, word]
    if run > lo and text[run - 1] == "%":
        starts.append(run - 1)
    return starts


def _unquoted_end(text: str, pos: int, depth: int) -> int:
    """Where an unquoted value whose separator is at `depth` ends, given that
    nothing before `pos` ends it."""
    for stop in _UNQUOTED_STOP.finditer(text, pos):
        if stop.group("enc") is None or _depth(stop) <= depth:
            return stop.start()
    return len(text)


def _match_past(
    text: str, lo: int, k: int, end: int, endpos: int | None = None
) -> re.Match[str] | None:
    """A :data:`_SECRET_VALUE` match for a name ending at `k` whose value runs
    past `end`, reading `text` only as far as `endpos`."""
    if k <= lo or not _is_name_char(text[k - 1], dash=False):
        return None
    for start in _name_starts(text, lo, k):
        m = _SECRET_VALUE.match(text, start, len(text) if endpos is None else endpos)
        if m is not None and m.end("kv_value") > end:
            return m
    return None


def _hidden_values(text: str, outer: re.Match[str], reach: list[int]) -> list[Span]:
    """The values of names inside `outer`'s value that run on past its end.

    `finditer` resumes where a match ends, so a name inside a value never
    starts a match of its own, and three shapes let that name's value outlast
    the one it hides in. In `token%3Apassword =S3CR` the space ends the
    `token` value and is also where `password`'s separator begins: the
    prefix of the hidden match runs through the outer value's end. In
    `token%3Apassword=a%26b` the `%26` ends the encoded `token` value but
    not `password`'s, whose raw `=` makes a value that only whitespace, a
    quote or a raw `&` ends. In `sig="x token:'a" S3CR'` the hidden value is
    quoted with a kind the outer one is not, and runs to its own `'`.

    Each is looked for only where it can be, so the search stays linear. A
    prefix running through the end is found by stepping back from the end
    over a space, a separator, a space and a closing quote, and a quoted
    hidden value by the same steps back from each quote inside a quoted outer
    one. No backslash precedes an opening quote, so a hidden value ends no
    later than the next opening quote of its own kind and the values of one
    kind are read without overlap. A shallower
    separator matters only when an encoded `&` ended the outer value, and
    only the shallowest one that names a secret, since its value ends no
    earlier than any deeper one's. `reach` is the furthest such value found
    so far and the depth of what ended it; a value inside it that the same
    `&` would end is already masked, and is not read a second time. One that
    runs past it does so through all of it, so it is read on from there:
    each `&` takes a separator one level shallower to pass, and re-reading
    the whole of each value made a line of `n` levels cost `n` passes. A value
    an `&` ended is masked whole; what is lost is only the run of names past
    it, which the rule cannot tell from more of the value."""
    value_start, end = outer.span("kv_value")
    lo = outer.start()
    found: list[Span] = []
    # Of the characters that end a value, only a space or a quote can also
    # be part of a prefix.
    crossable = end < len(text) and (text[end].isspace() or text[end] in "\"'")
    prefix_ends = [end] if crossable else []
    if outer.group("quote") is not None:
        # A quoted value may hold a quote of another kind, and a name's value
        # that one opens runs on to its own closing quote: in
        # `sig="x token:'a" S3CR'` it outlasts the `"` ending `sig`'s. Only
        # a prefix ending just before such a quote can open one.
        prefix_ends += [j for j in range(value_start, end) if text[j] in "\"'"]
    for prefix_end in prefix_ends:
        after_sep = _back_over_space(text, value_start, prefix_end)
        for before_sep in (after_sep, _back_over(text, value_start, after_sep, "=:", ("3a", "3d"))):
            if before_sep is None:
                continue
            k = _back_over_space(text, value_start, before_sep)
            for name_end in (k, _back_over(text, value_start, k, "\"'", ("22", "27"))):
                if name_end is not None and (m := _match_past(text, lo, name_end, end)) is not None:
                    found.append(m.span("kv_value"))
    amp = _ENCODED_AMP.match(text, end)
    if outer.group("pct") is None or amp is None:
        return found
    shallower = sorted(
        (_depth(sep), sep.start())
        for sep in _SEPARATOR.finditer(text, value_start, end)
        if _depth(sep) < _depth(amp)
    )
    for depth, sep_start in shallower:
        if end < reach[0] and reach[1] <= depth:
            break
        quote = _back_over(text, value_start, sep_start, "\"'", ("22", "27"))
        for name_end in (sep_start, quote):
            # The outer value holds no quote, no space and no `&` this
            # separator stops at, and neither is `amp` one, so only the name
            # needs reading: the value is unquoted, and runs past `end`.
            if (
                name_end is not None
                and (m := _match_past(text, lo, name_end, end, end + 1)) is not None
            ):
                value_end = _unquoted_end(text, max(end, reach[0]), depth)
                found.append((m.start("kv_value"), value_end))
                stop = _ENCODED_AMP.match(text, value_end)
                reach[:] = [value_end, _depth(stop) if stop is not None else _EVERY]
                return found
    return found


def _value_spans(text: str) -> list[Span]:
    """Where in `text` the name rule finds a secret value: each value a
    :data:`_SECRET_VALUE` key names, each one a name inside that value names
    (:func:`_hidden_values`), and each token :data:`_BEARER_VALUE` finds — plus,
    where there is a Bearer, each value :data:`_SECRET_VALUE_PAST_BEARER` finds.
    They may overlap."""
    spans: list[Span] = []
    reach = [0, _EVERY]
    for m in _SECRET_VALUE.finditer(text):
        spans.append(m.span("kv_value"))
        spans += _hidden_values(text, m, reach)
    bearers = [m.span("value") for m in _BEARER_VALUE.finditer(text)]
    if bearers:
        spans += [
            m.span("kv_value")
            for m in _SECRET_VALUE_PAST_BEARER.finditer(text)
            if m.group("kv_prefix") is not None
        ]
    return spans + bearers


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
