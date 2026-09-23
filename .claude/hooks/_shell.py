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


def tokens(text: str) -> list[str] | None:
    """`text` split the way a shell splits a command, with the separators,
    redirections and heredoc markers kept as tokens of their own — or None
    when a quote it opens is never closed, which a shell answers by reading
    on."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
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
    substitution rather than a subshell. shlex splits `$(` into `$` and `(`,
    and the `$` left behind is punctuation rather than an operand."""
    if command.argv and command.argv[-1] == "$":
        command.argv.pop()
        return True
    return False


def _read_line(line_tokens: list[str]) -> tuple[list[Command], str, Command | None]:
    """The commands in one readable line, the heredoc delimiter it leaves
    open, and the command that opened it.

    A redirection's file descriptor arrives as a token of its own, because
    shlex splits `2>` into `2` and `>`. It is dropped with the redirection,
    so it cannot sit in the argv and shift a positional past the one a caller
    is reading — and it is what tells `2>` from `>`, which is the difference
    between output a caller can still see and output it cannot.
    """
    commands = [Command()]
    delimiter = ""
    owner: Command | None = None
    redirect = ""
    substitution = False
    for token in line_tokens:
        for part in split_cluster(token):
            if redirect:
                if redirect == HEREDOC:
                    delimiter, owner = part.lstrip("-"), commands[-1]
                redirect = ""
            elif part in OPERATORS:
                commands[-1].stdout_to_pipe |= part in PIPES
                if part == "(":
                    substitution = _opens_substitution(commands[-1]) or substitution
                elif part == ")":
                    substitution = False
                commands.append(
                    Command(stdin_from_pipe=part in PIPES, in_substitution=substitution)
                )
            elif part in REDIRECTS:
                descriptor = ""
                if commands[-1].argv and commands[-1].argv[-1].isdigit():
                    descriptor = commands[-1].argv.pop()
                if part in WRITES_STDOUT and descriptor in ("", "1"):
                    commands[-1].stdout_to_file = True
                redirect = part
            else:
                substitution = _feed_word(commands, part, substitution)
    return [command for command in commands if command.argv], delimiter, owner


def read(cmd: str) -> Reading:
    """Every command `cmd` runs, and the tail of it that stayed unreadable.

    A heredoc body is collected onto the command that opened it rather than
    read as commands: its lines are text until that command is known to be a
    shell. A trailing backslash inside one is text too — joining it to the
    next line would swallow the terminator and read the rest of the command
    as more body. A line that leaves a quote open is joined to the next
    instead, which is what a shell does with it.
    """
    commands: list[Command] = []
    delimiter = ""
    owner: Command | None = None
    body: list[str] = []
    pending = ""
    for line in cmd.splitlines():
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ""
                if owner is not None:
                    owner.heredoc_body = "\n".join(body)
                owner, body = None, []
            else:
                body.append(line)
            continue
        pending = f"{pending}\n{line}" if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        line_tokens = tokens(pending)
        if line_tokens is None:
            continue
        pending = ""
        found, delimiter, owner = _read_line(line_tokens)
        commands.extend(found)
    if owner is not None:
        owner.heredoc_body = "\n".join(body)
    return Reading(commands, pending)
