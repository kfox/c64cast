"""How the `PreToolUse(Bash)` hooks read a command line.

Every hook that decides about a `Bash` call answers the same question first:
which commands does this line actually run? `shlex.split` cannot answer it.
Punctuation is not a token there, so `echo hi;grep -rn x docs/` lexes
`hi;grep` as one word, no command boundary is found, and a hook anchored on a
segment's leading command never sees the `grep`. Four hooks carried four
copies of that split and the copies had drifted; this is the one copy.

`read()` returns one `Command` per command on the line, each carrying the
facts about its wiring that some hook needs:

* `stdin_from_pipe` — a search fed by a pipe has no filesystem target at all,
  so it can never be the unresolvable-target shape.
* `stdout_to_pipe` / `stdout_to_file` — output that goes to `head` or to a
  file does not reach the context window, whatever its volume.
* `in_substitution` — the command inside `$(…)` or backticks. The enclosing
  command's operands are incomplete once its substitution has been lifted
  out, so a hook whose costly direction is a false deny declines to read a
  line containing one.
* `heredoc_body` — the text a command's `<<` collects. That body is prose
  when the command is `record` and a script when it is `bash`, and only the
  command that opened it can tell the two apart.

`Reading.unreadable` is the tail no lexer could take apart, which a caller
that refuses on doubt matches against as words rather than tokens.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

BACKTICK = "`"
HEREDOC = "<<"

# What `shlex(punctuation_chars=True)` gathers into a token of its own.
PUNCTUATION = "();<>|&"

OPERATORS = frozenset({"&", "&&", ";", ";;", "|", "||", "|&", "(", ")"})
REDIRECTS = frozenset({"<", "<&", "<<", "<<<", "<>", ">", ">&", ">>", ">|", "&>", "&>>"})
PIPES = frozenset({"|", "|&"})
WRITES_STDOUT = frozenset({">", ">>", ">|", ">&", "&>", "&>>"})

# The one write whose operand may be a descriptor instead of a file: `>&2`
# hands stdout to stderr, which the caller still reads back.
DUPLICATES = frozenset({">&"})

# A file descriptor in front of a redirection is a digit run glued to it, and
# a shell reads the two spellings differently: `2>f` sends stderr to the
# file, `-A 3 > f` passes `3` to the command and sends stdout. shlex renders
# them identically — `2`, `>` either way — so the glued form is marked before
# lexing, and only a marked token is taken out of the argv as a descriptor.
DESCRIPTOR_MARK = "\x00"

# The digit run has to be a word of its own for a shell to read it as a
# descriptor: `echo a2>f` passes `a2` and redirects stdout. `[0-9]` rather
# than `\d`, which also matches `٢` and `２` — a shell hands those to the
# command and sends stdout to the file.
_GLUED_DESCRIPTOR = re.compile(r"(?<![^\s|&;()<>])([0-9]+)(?=[<>])")

# Longest first, so a greedy walk over a glued run of punctuation prefers
# `&&` to two `&` and `<<` to two `<`.
_CLUSTER_PARTS = tuple(sorted(OPERATORS | REDIRECTS, key=len, reverse=True))

# Words a command can sit behind that are not commands themselves. `time` is
# a real binary as well as a keyword, and peeling it is the wanted answer
# either way.
KEYWORDS = frozenset(
    {
        "!",
        "{",
        "}",
        "do",
        "done",
        "elif",
        "else",
        "fi",
        "if",
        "then",
        "time",
        "until",
        "while",
    }
)


@dataclass
class Command:
    """One command on a line, and how the shell wired it up."""

    argv: list[str] = field(default_factory=list)
    stdin_from_pipe: bool = False
    stdout_to_pipe: bool = False
    stdout_to_file: bool = False
    in_substitution: bool = False
    heredoc_body: str = ""


@dataclass
class Reading:
    """Every command `read()` found, and the tail it could not take apart."""

    commands: list[Command] = field(default_factory=list)
    unreadable: str = ""

    def has_substitution(self) -> bool:
        return any(command.in_substitution for command in self.commands)


def _unmarked(token: str) -> str:
    """`token` with the descriptor mark kept only where it says something.

    On a bare digit run it is the difference between `2>f` and `2 > f`.
    Anywhere else the mark landed inside a quoted literal, where the
    redirection it sat in front of is text — so it comes back out and the
    token reads as it was written.
    """
    if token.endswith(DESCRIPTOR_MARK) and token[:-1].isdigit():
        return token
    return token.replace(DESCRIPTOR_MARK, "")


def tokens(text: str) -> list[str] | None:
    """`text` split the way a shell splits a command, with the separators,
    redirections and heredoc markers kept as tokens of their own — or None
    when a quote it opens is never closed, which a shell answers by reading
    on.

    A descriptor glued to a redirection keeps its mark, because nothing after
    lexing can tell it from an operand that happens to be a number.
    """
    marked = _GLUED_DESCRIPTOR.sub(rf"\1{DESCRIPTOR_MARK}", text)
    lexer = shlex.shlex(marked, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return [_unmarked(token) for token in lexer]
    except ValueError:
        return None


def split_cluster(token: str) -> list[str]:
    """`token` split into the operators glued together in it.

    shlex hands back a run of punctuation whole — `;(` rather than `;` and
    `(` — which matches no operator and leaves the commands on either side of
    it in one argv. A token that is not punctuation through and through is a
    word, and comes back unchanged.
    """
    if not token or any(character not in PUNCTUATION for character in token):
        return [token]
    parts: list[str] = []
    rest = token
    while rest:
        for candidate in _CLUSTER_PARTS:
            if rest.startswith(candidate):
                parts.append(candidate)
                rest = rest[len(candidate) :]
                break
        else:
            return [token]
    return parts


def strip_prefix(argv: list[str]) -> list[str]:
    """`argv` without the leading `VAR=value` assignments and shell keywords
    a command can hide behind."""
    while argv and (
        argv[0] in KEYWORDS or ("=" in argv[0] and argv[0].split("=", 1)[0].isidentifier())
    ):
        argv = argv[1:]
    return argv


def _feed_word(commands: list[Command], token: str, substitution: bool) -> bool:
    """`token` added to the command being built, opening or closing a
    backtick substitution at each backtick in it.

    shlex leaves a backtick glued to the word it touches, so `` `git `` and
    `` foo` `` arrive as single tokens. Splitting on the character turns each
    one into a command boundary, which is what the shell makes of it.
    """
    if BACKTICK not in token:
        commands[-1].argv.append(token)
        return substitution
    for index, piece in enumerate(token.split(BACKTICK)):
        if index:
            substitution = not substitution
            commands.append(Command(in_substitution=substitution))
        if piece:
            commands[-1].argv.append(piece)
    return substitution


def _opens_substitution(command: Command) -> bool:
    """Whether a `(` follows a `$`, making the command after it a
    substitution rather than a subshell.

    shlex splits the `(` off but leaves the `$` on whatever word it touches,
    so `$(` arrives as a bare `$` and `files=$(` as `files=$`. Both open a
    substitution, and reading only the bare one leaves `VAR=$(…)` looking
    like a plain command whose output reaches the caller.
    """
    if not command.argv or not command.argv[-1].endswith("$"):
        return False
    rest = command.argv[-1][:-1]
    if rest:
        command.argv[-1] = rest
    else:
        command.argv.pop()
    return True


@dataclass
class _Group:
    """A `(…)` or `{…}` under way: where its first command sits, what opened
    it, whether a `$` made it a substitution, and — once it closes — where it
    ended."""

    start: int
    opener: str
    substitution: bool = False
    end: int = 0


@dataclass
class _Pending:
    """What the reader carries from one line to the next: a heredoc the line
    has not closed, the command that opened it, whether it ends inside a
    `$(…)`, every command read so far, and the groups around them."""

    delimiter: str = ""
    owner: Command | None = None
    substitution: bool = False
    commands: list[Command] = field(default_factory=list)
    open_groups: list[_Group] = field(default_factory=list)
    closed_group: _Group | None = None


def _writers(pending: _Pending, line_start: int) -> list[Command]:
    """The commands whose output a pipe or a redirection carries.

    Usually one: the command the operator sits behind. A group is the
    exception, because it leaves a placeholder behind it — `)` starts a
    command no word ever reaches, and a closing `}` is a keyword rather than
    a command — and its stdout is the stdout of *every* command inside it. So
    `{ grep -r … ; echo done; } | head` pipes the grep as much as the echo,
    and marking one of them alone leaves the other looking unpiped.

    A `$(…)` is a group whose output goes into the enclosing command's
    operands instead, so the operator belongs to that command, not to what
    ran inside the substitution.
    """
    commands = pending.commands
    if strip_prefix(commands[-1].argv):
        return [commands[-1]]
    group = pending.closed_group
    if group is not None:
        if group.substitution:
            enclosing = [c for c in commands[line_start : group.start] if strip_prefix(c.argv)]
            if enclosing:
                return enclosing[-1:]
        else:
            inside = [c for c in commands[group.start : group.end] if strip_prefix(c.argv)]
            if inside:
                return inside
    for command in reversed(commands[line_start:]):
        if strip_prefix(command.argv) and not command.in_substitution:
            return [command]
    return [commands[-1]]


def _close_group(pending: _Pending, opener: str, end: int) -> _Group | None:
    """The group `opener` opened, now that it has ended at `end`.

    A closer whose opener is not the one waiting on the stack belongs to no
    group this reader saw — a `}` that came out of quotes, or a line the
    caller handed over from inside a group. Popping for it would hand the
    next pipe a span no group ever covered, so it is read as no group at all.
    """
    if not pending.open_groups or pending.open_groups[-1].opener != opener:
        return None
    group = pending.open_groups.pop()
    group.end = max(end, group.start)
    return group


def _read_line(line_tokens: list[str], pending: _Pending) -> None:
    """One readable line added to `pending` — the commands it runs, and what
    it leaves open for the line after it.

    A redirection's file descriptor arrives as a token of its own, because
    shlex splits `2>` into `2` and `>`. It is dropped with the redirection,
    so it cannot sit in the argv and shift a positional past the one a caller
    is reading — and it is what tells `2>` from `>`, which is the difference
    between output a caller can still see and output it cannot. Only a token
    `tokens()` marked is dropped, so a number the command was passed stays in
    the argv: `-A 3 > f` writes stdout to a file and searches with three
    lines of context. The operand answers the same question from the other
    side: `>&2` names a descriptor rather than a file, and what goes there is
    read back too — and it carries a mark of its own when a redirection
    follows it (`>&2<f`), so the mark comes off before the operand is read.

    A group carries over: `(` and the `)` that closes it need not share a
    line. A *closed* group does not, because a newline ends a command the way
    a `;` does, and the redirection on the line after a group belongs to no
    part of it.
    """
    commands = pending.commands
    line_start = len(commands)
    commands.append(Command(in_substitution=pending.substitution))
    pending.closed_group = None
    redirect = ""
    to_file: list[Command] = []
    for token in line_tokens:
        for part in split_cluster(token):
            if redirect:
                operand = part.removesuffix(DESCRIPTOR_MARK)
                if redirect == HEREDOC:
                    pending.delimiter, pending.owner = operand.lstrip("-"), commands[-1]
                elif to_file and not (redirect in DUPLICATES and operand.isdigit()):
                    for command in to_file:
                        command.stdout_to_file = True
                redirect, to_file = "", []
            elif part in OPERATORS:
                if part in PIPES:
                    for command in _writers(pending, line_start):
                        command.stdout_to_pipe = True
                if part == "(":
                    substitution = _opens_substitution(commands[-1])
                    pending.open_groups.append(_Group(len(commands), "(", substitution))
                    pending.closed_group = None
                    if substitution:
                        pending.substitution = True
                elif part == ")":
                    pending.closed_group = _close_group(pending, "(", len(commands))
                    pending.substitution = False
                else:
                    pending.closed_group = None
                commands.append(
                    Command(stdin_from_pipe=part in PIPES, in_substitution=pending.substitution)
                )
            elif part in REDIRECTS:
                descriptor = ""
                if commands[-1].argv and commands[-1].argv[-1].endswith(DESCRIPTOR_MARK):
                    descriptor = commands[-1].argv.pop()[: -len(DESCRIPTOR_MARK)]
                redirect = part
                to_file = (
                    _writers(pending, line_start)
                    if part in WRITES_STDOUT and descriptor in ("", "1")
                    else []
                )
            else:
                if part == "{":
                    pending.open_groups.append(_Group(len(commands) - 1, "{"))
                    pending.closed_group = None
                elif part == "}":
                    pending.closed_group = _close_group(pending, "{", len(commands) - 1)
                pending.substitution = _feed_word(commands, part, pending.substitution)


def read(cmd: str) -> Reading:
    """Every command `cmd` runs, and the tail of it that stayed unreadable.

    A heredoc body is collected onto the command that opened it rather than
    read as commands: its lines are text until that command is known to be a
    shell. A trailing backslash inside one is text too — joining it to the
    next line would swallow the terminator and read the rest of the command
    as more body. A line that leaves a quote open is joined to the next
    instead, which is what a shell does with it. A `$(` the line does not
    close carries over as well, so the commands under it are still known to
    be inside a substitution, and so does a group, so that a pipe on the line
    that closes one reaches back to what ran inside it.
    """
    body: list[str] = []
    pending = _Pending()
    unread = ""
    for line in cmd.splitlines():
        if pending.delimiter:
            if line.strip() == pending.delimiter:
                pending.delimiter = ""
                if pending.owner is not None:
                    pending.owner.heredoc_body = "\n".join(body)
                pending.owner, body = None, []
            else:
                body.append(line)
            continue
        unread = f"{unread}\n{line}" if unread else line
        if unread.endswith("\\"):
            unread = unread[:-1]
            continue
        line_tokens = tokens(unread)
        if line_tokens is None:
            continue
        unread = ""
        _read_line(line_tokens, pending)
    if pending.owner is not None:
        pending.owner.heredoc_body = "\n".join(body)
    return Reading([command for command in pending.commands if command.argv], unread)
