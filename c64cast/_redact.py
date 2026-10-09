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

:func:`redact_secrets` is a tokenizer rather than a set of patterns. Each line
is percent-decoded to a fixed point, every character remembering how many
decodings produced it (its *depth*) and which source characters it came from.
The decoded line is read for three things — a secret-shaped key and its value,
an auth scheme and its credential, and URL userinfo — each one found wherever
it starts, including inside another one's value, and the union of what they
cover is masked in the source text. A structure's own delimiters are the ones
no deeper than it is: a value whose `=` was encoded once ends at a `%26` or a
raw `&`, and keeps a `%2526`, which is an `&` inside the secret.
"""

from __future__ import annotations

import bisect
import functools
import itertools
import re
from array import array
from collections.abc import Iterator, Sequence

#: What a redacted value is replaced with. Deliberately not the same length as
#: any real token — a fixed-width mask invites the reader to guess.
REDACTED = "REDACTED"

Span = tuple[int, int]

#: A name a key ends with. `open` names take any prefix, glued or not
#: (`viewer_token`, `dbpasswd`, `userpass`); the rest only a prefix that ends in
#: `_` or `-`, because glued they are the tails of ordinary words: `sortkey`,
#: `monkey`, `sigma`, `oauth`. The `(?![\w-])` makes the name end where the
#: key does, so `passes=` and `jwt_expiry_s=` keep their values. Only `open` and
#: `header` names take a trailing number, glued or after a `_` or `-`
#: (`password2`, `token_1`): a numbered secret is as secret as the first, while
#: `key2` and `sig2` are as likely to be a column or an index.
_NAME = re.compile(
    r"""
    (?:
        (?:
            (?P<open>
                token | passw (?:or)? d | pass (?:phrase|code)? | loginpass? | pwd | jwt
              | secret | credentials? | api [_-]? key
              | (?: auth | priv (?:ate)? | access | secret | master | session | signing | stream ) key
            )
          | (?P<header> authorization )
        ) (?: [_-]? \d++ )?+
      | (?P<scheme> bearer )
      | (?P<short> key | sig (?:nature)? | hmac | auth )
    ) (?![\w-])
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Words that end in `pass` and name no secret: audio filters, encoder passes,
#: and the English words. Compared with `_` and `-` removed, so `high-pass` and
#: `bypass_audio` are both read here, but only from the start of a component:
#: see :func:`_names_no_password`.
_NOT_A_PASSWORD = (
    "allpass",
    "bandpass",
    "bypass",
    "compass",
    "encompass",
    "firstpass",
    "highpass",
    "lowpass",
    "multipass",
    "onepass",
    "overpass",
    "secondpass",
    "singlepass",
    "surpass",
    "trespass",
    "twopass",
    "underpass",
)
_NOT_A_PASSWORD_REACH = max(map(len, _NOT_A_PASSWORD)) * 2

#: The shell's working-directory variables, which the open `pwd` would mask.
#: Matched case-sensitively: a camera URL's `pwd=` is lowercase.
_SHELL_PWD = ("PWD", "OLDPWD")

#: What follows a key: an optional closing quote (escaped, once or more, in a
#: rendering such as `{\'token\': …}`), then `=`, `:` or `=>`, then the value.
#: The `>` is the separator's only when a space or a quote follows it; glued to
#: more text it is the value's first character, as in `password=>abc`. The
#: runs are possessive because none can give a character back to what follows
#: it, so backtracking into one is wasted work, and polynomial work to a
#: static ReDoS check.
_KEY_TAIL = re.compile(
    r"""\\*+ ["']? \s*+ (?P<sep> [=:] ) [=:]*+ (?: > (?= [\s"'\\] ) )? \s*""", re.VERBOSE
)

#: A quote that opens a value: single or triple, after an optional Python
#: string prefix (`b'…'`) or backslashes (an escaped rendering, perhaps
#: escaped again).
_OPENER = re.compile(
    r"""(?P<prefix> [bBrRuUfF]{0,2} ) (?P<esc> \\+ )? (?P<q> "{3} | '{3} | ["'] )""", re.VERBOSE
)

#: The string prefixes Python accepts, lowercased.
_STRING_PREFIXES = frozenset({"", "b", "r", "u", "f", "br", "rb", "fr", "rf"})

#: What separates an auth scheme from its credential: whitespace at any depth,
#: or the `+` a form-encoded header spells a space with.
_SCHEME_GAP = re.compile(r"[\s+]+")

#: The schemes an `Authorization` value keeps in view: the IANA HTTP
#: Authentication Scheme Registry, GitHub's `token` and AWS's signature scheme.
#: Keeping any first word would keep a bare credential that more text follows,
#: since that reads as a scheme and its credential.
_AUTH_SCHEMES = frozenset(
    {
        "aws4-hmac-sha256",
        "basic",
        "bearer",
        "concealed",
        "digest",
        "dpop",
        "gnap",
        "hoba",
        "mutual",
        "negotiate",
        "ntlm",
        "oauth",
        "privatetoken",
        "scram-sha-1",
        "scram-sha-256",
        "token",
        "vapid",
    }
)

_SCHEME_REACH = max(map(len, _AUTH_SCHEMES))

#: An `Authorization:` value that is a scheme and a credential, with any
#: punctuation around the scheme. Without it, `(Basic x)`, `(Basic) x`,
#: `s3cr3t, x` or a `%22` too deep to open a quote ends the value at the first
#: word and leaves the credential in view. Neither run takes `.` or `-`, nor
#: the trailing one `+`, so none can trade characters with its neighbor, which
#: is quadratic on a long run of them. They are possessive for the reason
#: `_KEY_TAIL`'s are.
_SCHEME_AND_GAP = re.compile(
    r"[^\w\s.-]*+ (?P<scheme> [\w.-]++ ) [^\w\s.+-]*+ (?P<gap> [\s+]+ )", re.VERBOSE
)

#: The `://` of a URL, after a scheme character. Anchored on the separator and
#: only looking behind it: a pattern that matched the scheme itself is retried
#: from every offset of a run of scheme characters, which is quadratic.
_URL_SEPARATOR = re.compile(r"(?<=[A-Za-z0-9+.\-])://")

#: Each kind of place a value can end, as a pattern over the decoded line. A
#: quote or a `}` ends an unquoted value only when no letter or digit follows
#: it — the `'` in `it's` is part of the password.
_STOP_PATTERNS = {
    "unquoted": re.compile(r"""[\s&,] | ["'}] (?!\w)""", re.VERBOSE),
    "unquoted+": re.compile(r"""[\s&,+] | ["'}] (?!\w)""", re.VERBOSE),
    "space": re.compile(r"\s"),
    "gap": re.compile(r"[\s+]"),
    "netloc": re.compile(r"[\s/?#]"),
    "@": re.compile(r"@"),
}

_QUOTES = ('"', "'", '"""', "'''")


@functools.cache
def _stop_pattern(kind: str) -> re.Pattern[str]:
    """The pattern for a stop `kind`. A quote kind is the quote itself, with a
    leading backslash when a backslash does not escape it and a trailing `~`
    when it closes whatever follows it; without the `~`, no letter or digit
    may follow."""
    quote = kind.lstrip("\\").rstrip("~")
    if quote not in _QUOTES:
        return _STOP_PATTERNS[kind]
    return re.compile(re.escape(quote) + ("" if kind.endswith("~") else r"(?!\w)"))


_LINE_BREAK = re.compile(r"[\r\n]")

#: `%`, any run of `25`, then two hex digits: one source escape decoded as many
#: times as it takes, in one step. Decoding a level at a time instead costs a
#: pass over the line per level, and `%2525…` has as many levels as it has
#: pairs.
_ESCAPE_CHAIN = re.compile(r"%(?:25)*[0-9A-Fa-f]{2}")
_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_HEX = frozenset("0123456789abcdefABCDEF")


def _decode(line: str) -> tuple[str, array[int] | None, array[int] | None]:
    """`line` percent-decoded to a fixed point, the source offset each decoded
    character starts at (plus `len(line)` at the end), and the depth of each.
    The two arrays are None when nothing decoded."""
    if "%" not in line:
        return line, None, None
    parts: list[str] = []
    starts: array[int] = array("q")
    depth: array[int] = array("q")
    done = 0
    for m in _ESCAPE_CHAIN.finditer(line):
        a, b = m.span()
        if a > done:
            parts.append(line[done:a])
            starts.extend(range(done, a))
            depth.extend(bytes(a - done))
        parts.append(chr(int(line[b - 2 : b], 16)))
        starts.append(a)
        depth.append((b - a - 1) // 2)
        done = b
    if done == 0:
        return line, None, None
    parts.append(line[done:])
    starts.extend(range(done, len(line)))
    depth.extend(bytes(len(line) - done))
    decoded = "".join(parts)
    if _ESCAPE.search(decoded):
        decoded, starts, depth = _reduce(decoded, starts, depth)
    starts.append(len(line))
    return decoded, starts, depth


def _reduce(
    decoded: str, starts: array[int], depth: array[int]
) -> tuple[str, array[int], array[int]]:
    """Decode the escapes a first decoding put together out of decoded
    characters (`%25%34%31` is `%41`, then `A`), until none is left. Each
    character is pushed once and each reduction pops three for one, so this
    is linear however many levels the line nests."""
    out: list[str] = []
    out_starts: array[int] = array("q")
    out_depth: array[int] = array("q")
    for i, c in enumerate(decoded):
        out.append(c)
        out_starts.append(starts[i])
        out_depth.append(depth[i])
        while len(out) >= 3 and out[-3] == "%" and out[-2] in _HEX and out[-1] in _HEX:
            c = chr(int(out[-2] + out[-1], 16))
            d = max(out_depth[-3:]) + 1
            start = out_starts[-3]
            del out[-3:], out_starts[-3:], out_depth[-3:]
            out.append(c)
            out_starts.append(start)
            out_depth.append(d)
    return "".join(out), out_starts, out_depth


#: A depth no query asks for, padding the tree past the last stop.
_UNREACHABLE = 1 << 62


class _Stops:
    """The positions in a line where one kind of value can end, and the depth
    of each, answering "the first at or after `p` no deeper than `d`" in
    logarithmic time. A scan from each value's start would read the same
    stretch once per value that starts in it: `token=token=…` is quadratic."""

    def __init__(self, positions: list[int], depths: list[int] | None) -> None:
        self._pos = positions
        self._depth = depths
        self._tree: list[int] | None = None
        self._size = 0

    def first(self, p: int, d: int) -> int | None:
        i = bisect.bisect_left(self._pos, p)
        if i == len(self._pos):
            return None
        if self._depth is None or self._depth[i] <= d:
            return self._pos[i]
        j = self._first_shallow(i, d)
        return None if j is None else self._pos[j]

    def last(self, lo: int, q: int, d: int) -> int | None:
        """The last position in `[lo, q)` no deeper than `d`."""
        i = bisect.bisect_left(self._pos, q) - 1
        if i < 0 or self._pos[i] < lo:
            return None
        if self._depth is None or self._depth[i] <= d:
            return self._pos[i]
        j = self._last_shallow(i, d)
        return None if j is None or self._pos[j] < lo else self._pos[j]

    def _min_tree(self) -> list[int]:
        if self._tree is None:
            depths = self._depth or []
            size = 1 << max(len(depths) - 1, 0).bit_length()
            tree = [_UNREACHABLE] * (2 * size)
            tree[size : size + len(depths)] = depths
            for k in range(size - 1, 0, -1):
                tree[k] = min(tree[2 * k], tree[2 * k + 1])
            self._tree, self._size = tree, size
        return self._tree

    def _first_shallow(self, i: int, d: int) -> int | None:
        tree = self._min_tree()
        k = i + self._size
        while tree[k] > d:
            while k & 1:
                k >>= 1
            if k == 0:
                return None
            k += 1
        while k < self._size:
            k = 2 * k if tree[2 * k] <= d else 2 * k + 1
        return k - self._size

    def _last_shallow(self, i: int, d: int) -> int | None:
        tree = self._min_tree()
        k = i + self._size
        while tree[k] > d:
            while not k & 1:
                k >>= 1
            if k == 1:
                return None
            k -= 1
        while k < self._size:
            k = 2 * k + 1 if tree[2 * k + 1] <= d else 2 * k
        return k - self._size


class _Line:
    """One decoded line, with the depth of each character and the stop sets
    built from it on first use."""

    def __init__(self, text: str, depth: array[int] | None) -> None:
        self.text = text
        self._depth = depth
        self._stops: dict[str, _Stops] = {}
        self.params_scanned: Span = (0, 0)
        self.word_gaps: set[int] = set()

    def depth(self, i: int) -> int:
        return 0 if self._depth is None else self._depth[i]

    def deepest(self, a: int, b: int) -> int:
        return 0 if self._depth is None or a >= b else max(self._depth[a:b])

    def stop(self, kind: str, p: int, d: int) -> int:
        """Where a value of `kind` that starts at `p`, and is `d` deep, ends."""
        found = self.stops(kind).first(p, d)
        return len(self.text) if found is None else found

    def stops(self, kind: str) -> _Stops:
        stops = self._stops.get(kind)
        if stops is None:
            text = self.text
            escapable = kind.rstrip("~") in _QUOTES
            positions = [
                m.start()
                for m in _stop_pattern(kind).finditer(text)
                if not (escapable and _is_escaped(text, m.start()))
            ]
            depths = None if self._depth is None else [self._depth[p] for p in positions]
            stops = self._stops[kind] = _Stops(positions, depths)
        return stops


def _is_escaped(text: str, p: int) -> bool:
    """Whether an odd run of backslashes ends just before `p`."""
    i = p
    while i > 0 and text[i - 1] == "\\":
        i -= 1
    return (p - i) % 2 == 1


def _is_name_char(c: str) -> bool:
    return c.isalnum() or c in "_-"


def _secret_names(text: str, judge: str | None = None) -> Iterator[re.Match[str]]:
    """Each secret-shaped name in `text` that ends a run of name characters.
    Whether a match is glued or one of the exempt words is read from `judge`
    instead when given, a text of the same length as `text`."""
    judged = text if judge is None else judge
    pos = 0
    while (m := _NAME.search(text, pos)) is not None:
        if _names_a_secret(judged, m):
            yield m
            pos = m.end()
        else:
            pos = m.start() + 1


#: A JSON `\uXXXX` escape ending just before a name. Its last hex digit glues
#: to the name, so `\u0026sig=` would read as the word `u0026sig`.
_JSON_ESCAPE = re.compile(r"\\u(?P<hex>[0-9A-Fa-f]{4})\Z")


def _char_before(text: str, s: int) -> str:
    """The character before `s`, read through a JSON escape that ends there,
    or "" at the start of `text`."""
    if s == 0:
        return ""
    escape = _JSON_ESCAPE.search(text, max(0, s - 6), s)
    return text[s - 1] if escape is None else chr(int(escape.group("hex"), 16))


def _starts_word(text: str, s: int) -> bool:
    """Whether no name character comes before `s`."""
    c = _char_before(text, s)
    return c == "" or not _is_name_char(c)


def _is_glued(text: str, s: int) -> bool:
    """Whether the name at `s` continues a word, rather than following a
    separator, a `_` or `-`, or a JSON escape of any of them."""
    c = _char_before(text, s)
    return c != "" and _is_name_char(c) and c not in "_-"


def _names_a_secret(text: str, m: re.Match[str]) -> bool:
    s = m.start()
    glued = _is_glued(text, s)
    if m.group("open") is None:
        return not glued
    name = m.group("open").lower()
    if name == "pass":
        return not _names_no_password(text, s, m.end("open"))
    if name == "pwd" and m.end("open") == m.end():
        for word in _SHELL_PWD:
            lo = m.end() - len(word)
            if text[lo : m.end()] == word and _starts_word(text, lo):
                return False
    return True


def _names_no_password(text: str, s: int, end: int) -> bool:
    """Whether the key ending in the `pass` at `s` ends with one of
    `_NOT_A_PASSWORD`, starting where one of the key's `_`/`-` components
    does. A plain suffix test on the joined run reads `firewall_pass` as
    `allpass` and `phone_pass` as `onepass`. A run longer than the reach is
    not known to start a component where the window does, so it keeps its
    mask."""
    lo = s
    while lo > 0 and s - lo < _NOT_A_PASSWORD_REACH and _is_name_char(text[lo - 1]):
        lo -= 1
    first = 0 if lo == 0 or not _is_name_char(text[lo - 1]) else 1
    if (
        first == 0
        and lo > 0
        and text[lo - 1] == "\\"
        and lo + 5 <= s
        and not _is_glued(text, lo + 5)
    ):
        # The run opens with the tail of a JSON escape of a separator: read
        # as part of the key, the `u0026` of `\u0026bypass` hid the word.
        lo += 5
    parts = re.split(r"[_-]", text[lo:end].lower())
    return any("".join(parts[i:]) in _NOT_A_PASSWORD for i in range(first, len(parts)))


def _opener(text: str, p: int) -> re.Match[str] | None:
    """The quote opening a value at `p`, after a prefix Python would accept.
    Any other letters before a quote begin an unquoted value: read as a
    prefix, the `rU` of `rU'secret` would stay in view."""
    m = _OPENER.match(text, p)
    return m if m is not None and m.group("prefix").lower() in _STRING_PREFIXES else None


def _value(line: _Line, v: int, d: int, kind: str) -> Span | None:
    """The span of the value starting at `v`, quoted or not; an unquoted one
    ends at a `kind` stop no deeper than `d`, which is its separator's depth."""
    opener = _opener(line.text, v)
    if opener is None and (shaped := _OPENER.match(line.text, v)) is not None:
        # Letters no Python prefix spells, then a quote: the letters start the
        # value, and the quote may still have opened it, so the mask runs to
        # whichever end is later. Read as unquoted alone, `rU'abc def'` left
        # `def'` in view.
        quoted = _quoted_end(line, shaped, shaped.end())
        if line.deepest(v, shaped.end()) > d:
            # As for a deep quote after a prefix below: `rU%27a b%27-tail`
            # otherwise kept `%27-tail`.
            return (v, line.stop(kind, quoted, d))
        return (v, max(quoted, line.stop(kind, v, d)))
    if opener is not None:
        start = opener.end()
        end = _quoted_end(line, opener, start)
        if line.deepest(v, start) > d:
            # A quote deeper than the separator may be the value's own
            # character (`pwd=R%22x%22-tail`) or a quote a log line encoded
            # (`token:%27a b%27`). Read as either alone, the other one's tail
            # stays in view, so the value runs past the closing quote to the
            # separator's own next stop.
            return (v, line.stop(kind, end, d))
        if opener.group("prefix") and end == len(line.text):
            # Nothing closed it, so the letters are not known to be a prefix:
            # they may be the secret's own first characters.
            return (v, end)
        return (start, end) if end > start else None
    end = line.stop(kind, v, d)
    return (v, end) if end > v else None


def _quoted_end(line: _Line, opener: re.Match[str], start: int) -> int:
    """Where the value `opener` opens ends: at its quote no deeper than the
    opening one, with no letter or digit after it — `'it's@er2'` is one value.
    Once the value has held whitespace, though, any such quote ends it: a
    value is one word or it is prose, and in prose the `'` of a possessive
    would otherwise carry the mask on to the next quote a space follows."""
    q, k = opener.group("q"), line.depth(opener.start("q"))
    kind = q if opener.group("esc") is None else "\\" + q
    end = line.stop(kind, start, k)
    space = line.stops("space").first(start, k)
    if space is not None and space < end:
        end = min(end, line.stop(kind + "~", space, k))
    return end


def _credential(line: _Line, c: int, gap: str, d: int) -> Span | None:
    """The credential after an auth scheme, starting at `c`. Its first
    character is always part of it, so `Bearer ,abc` keeps nothing, and it
    ends at a `+` only when a `+` is what separated it from the scheme."""
    if c >= len(line.text):
        return None
    kind = "unquoted+" if "+" in gap else "unquoted"
    if _OPENER.match(line.text, c) is not None:
        return _value(line, c, d, kind)
    return (c, line.stop(kind, c + 1, d))


def _key_values(line: _Line) -> Iterator[Span]:
    text = line.text
    for name in _secret_names(text):
        tail = _KEY_TAIL.match(text, name.end())
        if name.group("scheme") is not None and tail is None:
            gap = _SCHEME_GAP.match(text, name.end())
            if gap is not None:
                span = _credential(line, gap.end(), gap.group(), line.deepest(*gap.span()))
                if span is not None:
                    yield span
            continue
        if tail is None:
            span = _flag_value(line, name)
            if span is not None:
                yield span
            continue
        v, d = tail.end(), line.depth(tail.start("sep"))
        span = _value(line, v, d, "unquoted")
        if name.group("header") is not None:
            span = _past_scheme(line, v, d, span)
        if span is not None:
            yield span


#: What separates a command-line flag from its value.
_FLAG_GAP = re.compile(r"[ \t]+")

#: What separates a quoted flag from its value: its closing quote, then a comma
#: or whitespace, then the quote opening the value, as in a list repr of an argv
#: (`['--password', 'hunter2']`). The quotes may be backslash-escaped, and the
#: value's may follow a string prefix (`b'hunter2'`). A value
#: that is no quoted string is not read: prose such as `the "--password", then`
#: would otherwise lose its next word.
_QUOTED_FLAG_GAP = re.compile(
    r"""
    (?P<close> \\*+ ["'] ) (?: [ \t]*+ , [ \t]*+ | [ \t]++ ) (?= [bBrRuUfF]{0,2}+ \\*+ ["'] )
    """,
    re.VERBOSE,
)


#: A dash, after the quote and string prefix that open a quoted list element.
_LISTED_FLAG = re.compile(r"""[bBrRuUfF]{0,2}+ \\*+ ["'] -""", re.VERBOSE)


def _flag_gap(text: str, run: int, end: int) -> tuple[Span, bool] | None:
    """The gap between the flag whose run of name characters starts at `run`
    and ends at `end`, and its value, and whether that value starts with a
    `-`, which makes it another flag."""
    gap = _FLAG_GAP.match(text, end)
    if gap is not None:
        return gap.span(), text.startswith("-", gap.end())
    gap = _QUOTED_FLAG_GAP.match(text, end)
    if gap is None:
        return None
    opening = run
    while opening > 0 and text[opening - 1] == "\\":
        opening -= 1
    if opening == 0 or text[opening - 1] != gap.group("close")[-1]:
        return None
    return gap.span(), _LISTED_FLAG.match(text, gap.end()) is not None


def _flag_value(line: _Line, name: re.Match[str]) -> Span | None:
    """The value after a flag such as `--password` or `--video-password` that
    `name` ends, given as the next word: a logged command line (yt-dlp's, say)
    spells it that way, and a list repr of an argv, as the next element
    (`['--password', 'hunter2']`). An `--authorization` value is a scheme and a
    credential, so it is read as an `Authorization:` header's is. A next word
    that is itself a flag is not a value, and a short name that is the whole
    flag is left out: `--key 3.0:…` is a keystroke and `C=-key pause` prose.
    After a component of its own it is kept, or `--stream-key X` would keep
    `X` where `--streamkey X` does not."""
    text = line.text
    run = name.start()
    while run > 0 and _is_name_char(text[run - 1]):
        run -= 1
    if name.group("short") is not None and not text[run : name.start()].strip("-"):
        return None
    found = _flag_gap(text, run, name.end())
    if text[run] != "-" or found is None:
        return None
    gap, is_flag = found
    if gap[1] == len(text) or is_flag:
        return None
    v, d = gap[1], line.deepest(*gap)
    span = _value(line, v, d, "unquoted")
    return _past_scheme(line, v, d, span) if name.group("header") is not None else span


def _past_scheme(line: _Line, v: int, d: int, span: Span | None) -> Span | None:
    """The credential in an `Authorization` value at `v`, `d` deep, whose
    `span` is what a value there would cover. A known scheme before it is
    kept, as `Bearer` is; any other first word may be the credential itself,
    so it goes with what follows it."""
    text = line.text
    opener = _opener(text, v)
    deep = opener is not None and line.deepest(v, opener.end()) > d
    if opener is not None and not deep:
        if span is None:
            return None
        scheme = _SCHEME_AND_GAP.match(text, span[0], span[1])
        if scheme is None:
            return _past_quoted_scheme(line, opener, span) or span
        if scheme.end() == span[1] or not _is_auth_scheme(scheme):
            return span
        return (scheme.end(), span[1])
    # Read from past a deep quote's prefix: from `v`, the `b` of `b%22Basic%22`
    # is glued to the quote, no scheme matches, and the credential stays in view.
    scheme = _SCHEME_AND_GAP.match(text, v if opener is None else opener.end("prefix"))
    if scheme is None:
        word = _unknown_scheme(line, v if opener is None else opener.end("prefix"))
        if word is None or span is None:
            return span if word is None else word
        return (min(word[0], span[0]), max(word[1], span[1]))
    if scheme.end() == len(text):
        return span
    gap_start, gap_end = scheme.span("gap")
    credential = _credential(line, gap_end, scheme.group("gap"), line.deepest(gap_start, gap_end))
    if deep and span is not None and span[1] > gap_end:
        # A quote deeper than the separator is read both ways, as in
        # `_value`: bounded by the quoted span alone, `%22Basic%22 x` kept `x`.
        credential = (gap_end, max(span[1], gap_end if credential is None else credential[1]))
    if _is_auth_scheme(scheme):
        return _widen_digest(line, scheme.group("scheme"), credential, gap_end, d)
    return span if credential is None else (v, credential[1])


def _widen_digest(line: _Line, scheme: str, credential: Span | None, c: int, d: int) -> Span | None:
    """`credential`, the one that follows `scheme` at `c` in a value `d` deep,
    widened to the whole parameter list when the scheme is `Digest`."""
    if credential is None or scheme.lower() != "digest":
        return credential
    return (credential[0], max(credential[1], _params_end(line, c, d)))


def _past_quoted_scheme(line: _Line, opener: re.Match[str], span: Span) -> Span | None:
    """The credential after a quoted value that is exactly a registered scheme,
    when whitespace or a `+` follows its closing quote: `"Basic" ab rest`. The
    scheme stays in view, as it does with the credential inside the quotes.
    Closing quote and comma, as in `{'Authorization': 'Basic', 'next': 'v'}`,
    leave the value a lone word, which goes."""
    text, q = line.text, opener.group("q")
    end = span[1] - len(opener.group("esc") or "")
    if end - span[0] > _SCHEME_REACH or not text.startswith(q, span[1]):
        return None
    scheme = text[span[0] : end]
    if scheme.lower() not in _AUTH_SCHEMES:
        return None
    gap = _SCHEME_GAP.match(text, span[1] + len(q))
    if gap is None:
        return None
    d = line.deepest(*gap.span())
    return _widen_digest(line, scheme, _credential(line, gap.end(), gap.group(), d), gap.end(), d)


def _unknown_scheme(line: _Line, v: int) -> Span | None:
    """The first word of an `Authorization` value at `v`, and the word after
    it, when no scheme pattern read the word: a first word with punctuation
    inside it (`s3!x ab`) is no known scheme, and may be a credential that more
    text follows. The word runs to whitespace or a `+` and takes quotes inside
    it, but does not begin with one: `%22ab%22-tail rest` is a quoted value and
    its tail, and `rest` is not a credential. A word whose gap an earlier call
    read goes to the end of the line instead of reading the gap again."""
    text = line.text
    if v >= len(text) or text[v] in "\"'":
        return None
    end = line.stop("gap", v + 1, _UNREACHABLE)
    if end >= len(text):
        return None
    if end in line.word_gaps:
        return (v, len(text))
    line.word_gaps.add(end)
    gap = _SCHEME_GAP.match(text, end)
    if gap is None:
        return None
    credential = _credential(line, gap.end(), gap.group(), line.deepest(*gap.span()))
    return None if credential is None else (v, credential[1])


def _params_end(line: _Line, c: int, d: int) -> int:
    """Where the parameter list of a `Digest` credential starting at `c` ends
    in a value `d` deep: at the end of the line, at an `&` outside every quoted
    parameter that is no deeper than `d`, or at a quote that opens none, which
    is the quote closing the header. A parameter's quote opens it when an `=`
    comes before it, and closes at the next such quote with as many backslashes
    before it, so `response=\\"a\\"` inside a JSON string is read whole. The
    list holds spaces, commas and quotes, which end an ordinary value, so the
    response and the cnonce would be left in view. A `Digest` that starts
    inside a stretch already read ends at the end of the line instead of
    reading it again, which a run of `Digest` words would make quadratic."""
    text = line.text
    if line.params_scanned[0] < c < line.params_scanned[1]:
        return len(text)
    quote: tuple[str, int] | None = None
    backslashes = 0
    i = c
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            backslashes += 1
        else:
            if quote is None:
                if ch == "&" and line.depth(i) <= d:
                    break
                if ch in "\"'":
                    if i - backslashes - 1 < c or text[i - backslashes - 1] != "=":
                        i -= backslashes
                        break
                    quote = (ch, backslashes)
            elif (ch, backslashes) == quote:
                quote = None
            backslashes = 0
        i += 1
    else:
        i = len(text)
    line.params_scanned = (c, i)
    return i


def _is_auth_scheme(m: re.Match[str]) -> bool:
    return m.group("scheme").lower() in _AUTH_SCHEMES


def _userinfo(line: _Line) -> Iterator[Span]:
    """`user:pass` in `scheme://user:pass@host`, up to the netloc's last `@`,
    so a password holding a raw `@` goes too. Whitespace ends the netloc — a
    URL a program opened holds none, and an unbounded match would run from
    `tr://COM3` across a sentence to an e-mail address — and a quote does not:
    RFC 3986 allows a `'` in userinfo. An empty one is masked as well."""
    for m in _URL_SEPARATOR.finditer(line.text):
        p, d = m.end(), line.deepest(m.start(), m.end())
        netloc_end = line.stop("netloc", p, d)
        at = line.stops("@").last(p, netloc_end, d)
        if at is not None:
            yield (p, at)


def _line_spans(line: str) -> list[Span]:
    """The source spans of `line` (no line break in it) that hold a secret."""
    decoded, starts, depth = _decode(line)
    view = _Line(decoded, depth)
    spans = [*_key_values(view), *_userinfo(view)]
    if starts is None:
        return spans
    return [(starts[a], starts[b]) for a, b in spans]


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


def redact_secrets(text: str) -> str:
    """`text` with every recognized secret reduced to ``REDACTED``:

    * the value of a key whose name ends in `token`, `password`, `passwd`,
      `pass`, `passphrase`, `passcode`, `loginpas(s)`, `pwd`, `jwt`, `secret`,
      `credential(s)` or `apikey`, with or without a number after it
      (`password2`, `token_1`) — glued to any prefix, so `viewer_token` and
      `userpass` match, bar the words that end in `pass` and name nothing
      secret (`bypass`, `high-pass`, starting a component, so `firewall_pass`
      still matches) and the shell's `PWD` — or whose last `_`/`-` component
      is `key`, `sig`, `signature`, `hmac`, `auth` or `bearer`, so
      `signing_key` matches and `sortkey` does not, and a JSON escape of a
      separator (`\\u0026sig=`) counts as one. `=`, `:` or `=>`
      separates them, with the key quoted or not, or, after a flag's dash
      (`--password X`), a space or tab, except where a `_`/`-` name is the
      whole flag (`--key X`);
    * the credential after `Bearer` and a space, and in an `Authorization:`
      value or an `--authorization` flag's, after a registered scheme
      (`Basic`, `token`, …) and any punctuation around it, which stay in view;
      a first word that is no known scheme is masked with the rest,
      punctuation and all, and after `Digest` the whole parameter list goes,
      to the end of the line, an `&` outside a quoted parameter, or the quote
      closing the header;
    * the userinfo of a URL (`https://user:pass@host` comes back as
      `https://REDACTED@host`) — a private media file is legitimately reached
      that way, and FFmpeg quotes the URL it failed on into its errors.

    Every shape is read after percent-decoding, so `%26sig%3DVALUE` and
    `Bearer%20VALUE` are covered at any depth of encoding. An unquoted value
    ends at whitespace, `&` or a comma no deeper than its separator, or at a
    quote or `}` that no letter or digit follows. A quoted one — `'`, `"`,
    `'''` or `\"\"\"`, perhaps backslash-escaped or after a string prefix
    Python accepts (`b`, `rb`, …) — runs to the matching quote that no
    backslash escapes and no letter or digit follows; when nothing closes a
    prefixed one, the prefix letters are masked too. A quote deeper than the
    value's separator (`pwd=%22…`) runs to its match as well, and the value
    goes on from there to the separator's own next stop.

    Nothing crosses a line break: a value written across several lines is
    masked only as far as its first one. Masking a value means finding where it
    starts and ends, which a malformed line does not offer —
    :func:`redact_source_line` is for the caller quoting one of those."""
    spans: list[Span] = []
    start = 0
    for brk in itertools.chain(_LINE_BREAK.finditer(text), (None,)):
        end = len(text) if brk is None else brk.start()
        if end > start:
            spans += [(a + start, b + start) for a, b in _line_spans(text[start:end])]
        start = end + 1
    return _splice(text, _merge(spans)) if spans else text


_TRIPLE = ('"""', "'''")

#: `scheme://` followed by anything up to an `@` that is still inside the
#: netloc, for a line a parser *rejected*. Deliberately not `urlsplit`: such a
#: line may hold no parseable URL at all, and the point is to spot the *shape*
#: of userinfo without needing the line to be well formed. The netloc ends at
#: the first `/`, `?` or `#`, so those bound the search and an `@` later in a
#: path or query is not userinfo.
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
#: Unlike :func:`_userinfo`, whitespace does *not* bound it. A passphrase with
#: a space in it is precisely the malformed shape this runs on, and
#: `u64://kelly:my pass@host` would otherwise come back whole.
_URL_USERINFO = re.compile(r"://(?<=[a-z0-9+.\-]://)[^/?#]*@", re.IGNORECASE)

#: The characters a URL scheme is spelled with (RFC 3986 §3.1).
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")


def _scheme_start(line: str, separator: int) -> int:
    """The index in `line` where the scheme ending at the `://` found at
    `separator` begins."""
    i = separator
    while i > 0 and line[i - 1] in _SCHEME_CHARS:
        i -= 1
    return i


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


#: Read as a space when looking for a name on a rejected line.
_NAME_JOINERS = str.maketrans("-_", "  ")


def _first_name_end(line: str, decoded: str, starts: array[int] | None) -> int | None:
    """Where the earliest secret-shaped name on a rejected `line` ends, read
    as given and as `decoded` (whose characters start at `starts`), and each
    of those again with every `-` and `_` read as a space. Decoded, because
    :func:`redact_secrets` reads `%26sig%3D` as a name and the raw `6sig` is
    glued. With `-` and `_` as a space, because the rule that a name ends
    where its key does would otherwise keep `dma_password-"hunter2"` or
    `dma_password_"hunter2"` whole, and either key next to `=` typed for it
    is exactly the kind of line a parser refuses. Glue and the exempt words
    are still judged on the text as written, so `high-pass` stays a filter."""
    ends: list[int] = []
    readings = [(line, None)] if starts is None else [(line, None), (decoded, starts)]
    for text, source in readings:
        for view in (text, text.translate(_NAME_JOINERS)):
            if (name := next(_secret_names(view, text), None)) is not None:
                ends.append(name.end() if source is None else source[name.end()])
    return min(ends, default=None)


def _userinfo_cut(line: str, decoded: str, starts: array[int] | None) -> int | None:
    """Where the scheme of the first URL userinfo on a rejected `line` starts,
    read as given and as `decoded`: `https%3A%2F%2Fkelly:pw%40host` carries
    the same password as its decoded spelling."""
    cuts: list[int] = []
    if (raw := _URL_USERINFO.search(line)) is not None:
        cuts.append(_scheme_start(line, raw.start()))
    if starts is not None and (dec := _URL_USERINFO.search(decoded)) is not None:
        cuts.append(starts[_scheme_start(decoded, dec.start())])
    return min(cuts, default=None)


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

    * A line naming a secret — any name :func:`redact_secrets` keys on, or an
      auth scheme — keeps the name and loses everything after it. The name is
      the whole diagnostic — it says which setting the parser choked on — and
      no source text survives past it to be read. Everything, that is, unless
      the userinfo rule below cuts earlier.
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
    decoded, starts, _ = _decode(line)
    key_end = _first_name_end(line, decoded, starts)
    cut = _userinfo_cut(line, decoded, starts)
    if cut is not None and (key_end is None or cut < key_end):
        return f"{line[:cut]}{REDACTED}", False
    if key_end is not None:
        return f"{line[:key_end]} {REDACTED}", False
    return line, True
