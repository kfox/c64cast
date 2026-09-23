"""Keep the whole suite off the developer's own files.

The suite mocks all hardware, but nothing stopped it reaching the *developer's
own* files. Three locations are real and populated on a machine that actually
runs c64cast: `~/.config/c64cast/settings.toml` (read inside `config.load`, so
it silently overlays every "assert the defaults" test), `~/.local/share/c64cast/`
(DAC calibrations, WLED + loop presets, a dumped character ROM — all *written*
by their owners), and the gitignored media under `assets/`, which only exists
on the machine that put it there. A leak into any of them means a test's
verdict depends on the machine it ran on. `MachineSettingsIsolation` redirects
the first two, but it is opt-in per module, so the answer to "is the suite
hermetic?" was a list somebody had to keep — and 20 of 140 modules had already
drifted off it.

This turns that from a convention into an enforced property, in two halves:

* :func:`redirect_local_state` points the paths that have an override at a
  throwaway directory for the whole process, so no module has to opt in to
  being hermetic and none can forget to.
* :func:`arm` installs an audit hook that fails the test outright if anything
  still reaches a real file, under a rule broad enough to catch a leak nobody
  anticipated: **the suite may read and write the checkout, the temp
  directory, and the interpreter's own installation — nothing else under
  `$HOME`.** Two things are carved back *out* of the checkout. `assets/`,
  since everything there but the READMEs and the logo is gitignored. And
  `.git/`, which the rule above could never have caught, because the
  repository's own metadata is *inside* the checkout the suite is otherwise
  free to write — see :func:`git_env_conflict` for what that cost.

Why an audit hook for the second half rather than patching `builtins.open`:
the C-level opens in cv2, PyAV and sqlite3 don't go through it, and the paths
that matter are reached from a dozen unrelated call sites (`char_rom.resolve`,
`dac_calibration_store`, `config_store`, `console_library`, the transport's
loop presets), so there is no one seam to wrap. `sys.addaudithook` sits below
all of them and cannot be bypassed from Python.

Everything outside `$HOME` is allowed without enumeration, on purpose. Reading
`/etc/localtime`, `/dev/urandom` or a system font is not the hazard this
guards, and an allowlist that had to name them all would fail differently on
each of the three CI platforms.

Armed at interpreter startup by `tests/sitecustomize.py`; every entry point
puts `tests` on `PYTHONPATH` so that happens (`Makefile`,
`scripts/pre-commit.sh`, `scripts/coverage.sh`, and CI's own unit-test step —
`test_fs_sandbox.py` holds all four to it). It has to be `PYTHONPATH` rather
than a hook armed from a test module: `unittest_parallel` runs the modules in
worker processes, and only the environment reaches those whatever start method
multiprocessing picks.

Blind spots worth knowing:

* A violation is raised at the `open()` call site, so code that catches
  broadly can swallow it. `SandboxViolation` derives from `AssertionError` —
  reported as a plain test failure, and outside the `except OSError` most of
  the resolver chain uses — but an `except Exception` in front of a leak would
  hide it. The next unguarded access still reports.
* A path with no directory part goes unchecked, because it cannot be resolved
  here (see `_hook`).
* The `subprocess` half reads only a call whose argv is a list and whose
  program is named `git`. CPython hands the audit hook a list on POSIX and an
  already-flattened command line on Windows, so on Windows it reads nothing at
  all; a POSIX `shell=True` call arrives as `/bin/sh -c …` and is skipped like
  any other program that is not git, as is `os.system`. Parsing a shell word
  for `-C` is not the fix — quoting and `&&` make it guesswork — so a fixture
  that shells out to git strips the ambient `GIT_*` itself, with
  `no_inherited_git_env` from `tests/_fakes.py`.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from typing import NamedTuple

# Private marker that a process in this tree already set the suite up; not one
# of the two public overrides below.
_TAKEOVER_ENV = "_C64CAST_SUITE_ROOT"
_SETTINGS_ENV = "C64CAST_SETTINGS"
_DATA_DIR_ENV = "C64CAST_DATA_DIR"

# Filesystem audit events whose first argument is a path — not the complete set
# CPython raises. `open` covers every read and rewrite; the rest catch the directory
# and metadata operations that create, move or delete without opening. A
# `subprocess.Popen` is handled apart from these: its first argument is a program,
# and what is out of bounds about it is the environment it inherits.
_PATH_EVENTS = frozenset(
    {
        "open",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.listdir",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.scandir",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.move",
        "shutil.rmtree",
    }
)


class SandboxViolation(AssertionError):
    """A test reached a file the suite is not allowed to depend on."""


def _resolve(path: str) -> str:
    """`path` as an absolute, symlink-free path."""
    return os.path.realpath(os.path.abspath(path))


def _key(path: str) -> str:
    """A resolved path as a prefix-comparison key: a trailing separator so a
    sibling whose name merely starts the same way can't match (`/tmp/c64` vs
    `/tmp/c64cast`), case-folded because macOS and Windows both resolve
    case-insensitively and the same directory arrives spelled both ways."""
    return os.path.join(path, "").casefold()


# Resolved, not merely absolute: `violation` compares a realpath, so an
# unresolved CHECKOUT makes `_ASSETS` never match on a checkout reached through
# a symlink (ordinary on macOS) and the guard fails silently open.
CHECKOUT = _resolve(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _allowed_roots() -> tuple[str, ...]:
    """Prefix keys the suite may touch even though they sit under `$HOME`.

    The checkout is where the code, the fixtures and every generated artifact
    live. `tempfile.gettempdir()` is where a well-behaved test writes; `/tmp`
    and `/private/tmp` join it because macOS reports the same directory under
    two names depending on who asked. The four interpreter prefixes cover both
    the project venv and the interpreter itself, which `uv` and `mise` keep
    under `~/.local/share/`.
    """
    roots = [
        CHECKOUT,
        tempfile.gettempdir(),
        "/tmp",
        "/private/tmp",
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
    ]
    return tuple(sorted({_key(_resolve(r)) for r in roots}))


_HOME = _key(_resolve(os.path.expanduser("~")))
_ALLOWED = _allowed_roots()
_ASSETS = _key(os.path.join(CHECKOUT, "assets"))


#: Set while this module is reading a file to answer its *own* question. Kept
#: per thread rather than as a plain flag for the reason
#: :func:`allow_outside_checkout` gives for not disarming: the armed flag is
#: read by every thread, so a process-wide suspend would also un-police
#: whatever the code under test had running in the background.
_probing = threading.local()


@contextlib.contextmanager
def _own_read() -> Iterator[None]:
    """Exempt the reads this module makes to locate the git metadata.

    :func:`_git_dirs_at` has to read `<root>/.git` and the `commondir` beside
    it, and both land on paths :func:`violation` now refuses — so with the hook
    armed the guard's own probe raises, blaming the test for touching `.git`
    when what it did was shell out to `git`. That fires for any `-C` target
    whose metadata is this repository's: a sibling worktree, or the primary
    checkout, which is where every `-C` that is not under `CHECKOUT` but is
    still this repo ends up.
    """
    was = getattr(_probing, "active", False)
    _probing.active = True
    try:
        yield
    finally:
        _probing.active = was


class _RepoDirs(NamedTuple):
    """Where one checkout keeps its git metadata, as prefix-comparison keys.

    ``private`` is the gitdir that checkout alone uses — its ``HEAD``, its
    index, its refs. ``common`` is the metadata every worktree of the
    repository shares, and is where ``config`` lives. Outside a worktree the
    two are one directory. ``gitfile`` is the resolved ``<tree>/.git`` itself,
    kept unkeyed because in a worktree it is a *file*.
    """

    tree: str
    gitfile: str
    private: str
    common: str


def _repo_dirs_at(root: str) -> _RepoDirs:
    """Where the checkout at ``root`` keeps its git metadata.

    Usually ``<root>/.git`` is all of it. In a **worktree** that name is a file
    reading ``gitdir: <path>``, the metadata that worktree alone uses sits
    outside the tree entirely, and the ``commondir`` beside it names the
    primary checkout's ``.git``, where ``config`` actually lives. Every change
    in this repository is made in a worktree, so covering only the local name
    would leave the file that got written in #482 unguarded in the place the
    work happens.

    That "outside the tree entirely" is also why :func:`git_env_conflict`
    compares resolved repositories rather than asking whether ``GIT_DIR`` sits
    inside the ``-C`` target: in a worktree it never does, and a containment
    test would read the pre-commit hook's own ``GIT_DIR`` as a conflict with
    the very worktree it was exported for.

    Read off disk rather than asked of `git rev-parse`: this runs at
    interpreter startup in every `unittest_parallel` worker, and a subprocess
    per worker to learn a path that two small files already state is a cost for
    nothing.
    """
    local = os.path.join(root, ".git")
    gitfile = _resolve(local)
    private = common = gitfile
    with _own_read():
        try:
            with open(local, encoding="utf-8") as fh:
                marker = fh.read(4096).strip()
        except (OSError, ValueError, UnicodeDecodeError):
            marker = ""
        prefix = "gitdir:"
        if marker.startswith(prefix):
            private = common = _resolve(os.path.join(root, marker[len(prefix) :].strip()))
            with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
                with open(os.path.join(private, "commondir"), encoding="utf-8") as fh:
                    common = _resolve(os.path.join(private, fh.read().strip()))
    return _RepoDirs(_key(_resolve(root)), gitfile, _key(private), _key(common))


def _git_dirs_at(root: str) -> tuple[str, ...]:
    """Prefix keys for every directory holding the git metadata of the checkout
    at ``root``, which is what :func:`violation` has to refuse — the private
    and the shared half alike, since a test has no business in either."""
    dirs = _repo_dirs_at(root)
    return tuple(sorted({_key(dirs.gitfile), dirs.private, dirs.common}))


def _repo_dirs(resolved: str) -> _RepoDirs:
    """Where the repository at ``resolved`` keeps its metadata.

    A target inside this checkout answers from the values computed at import
    instead of reading `<target>/.git` again — the common case, and the only
    one the gates themselves hit. What keeps the read safe for the *other*
    targets is :func:`_own_read`; without it the probe lands on the metadata
    :func:`violation` refuses and the guard fails the call it meant to allow.
    """
    if _key(resolved).startswith((_REPO.tree, _REPO.private, _REPO.common)):
        return _REPO
    return _repo_dirs_at(resolved)


_REPO = _repo_dirs_at(CHECKOUT)
_GIT = tuple(sorted({_key(_REPO.gitfile), _REPO.private, _REPO.common}))
#: In a worktree `<checkout>/.git` is a *file*, so the prefix keys above —
#: which carry a trailing separator on purpose — cannot match it. Case-folded
#: for the same reason `_key` is: macOS and Windows resolve case-insensitively.
_GIT_FILES = frozenset({_REPO.gitfile.casefold()})
_armed = False
_exempt: tuple[str, ...] = ()


def asset_is_tracked(rel: str) -> bool:
    """Whether `rel` — a checkout-relative, forward-slashed path under
    `assets/` — is one of the files git actually carries.

    `assets/` is a working directory for local media: the dumped ROMs, the
    MediaPipe model, the sample videos and pictures are all gitignored (its
    README says so), and only a README per directory plus the logo are
    committed. Written as a rule rather than a list of ten paths, with
    `test_fs_sandbox.py` holding the rule and `git ls-files` to each other.
    """
    return os.path.basename(rel) == "README.md" or rel == "assets/logo.png"


def violation(path: str) -> str | None:
    """Why `path` is out of bounds, or None if the suite may touch it.

    Pure and importable on its own so the rule is unit-testable without arming
    anything — an audit hook cannot be uninstalled once added, so a test that
    had to arm one to exercise its logic could only ever run last.
    """
    try:
        resolved = _resolve(path)
    except (OSError, ValueError):  # unresolvable — nothing to police
        return None
    target = _key(resolved)
    if _exempt and target.startswith(_exempt):
        return None
    if target.startswith(_GIT) or resolved.casefold() in _GIT_FILES:
        return (
            f"test touched {path!r}, inside this checkout's own git metadata. A test has no "
            f"business reading or writing the repository's metadata, and the damage "
            f"does not look like a test failure: a fixture that reached .git/config "
            f"once left `user.name = Test` there and misattributed 17 real commits "
            f"across four branches before anyone noticed. Build a scratch repo under "
            f"tempfile.mkdtemp() and strip the ambient GIT_* from its environment — "
            f"see tests/_fakes.py."
        )
    if target.startswith(_ASSETS):
        rel = os.path.relpath(resolved, CHECKOUT).replace(os.sep, "/")
        if asset_is_tracked(rel):
            return None
        return (
            f"test reached {rel!r}. Everything under assets/ but the READMEs "
            f"and the logo is gitignored, so it exists on the machine that put "
            f"it there and nowhere else — a test that depends on one asserts "
            f"something different on CI. Point the code under test at a file "
            f"the test writes under tempfile.mkdtemp() instead."
        )
    if not target.startswith(_HOME) or target.startswith(_ALLOWED):
        return None
    return (
        f"test touched {path!r}, which is outside the checkout and the temp "
        f"directories. The suite must not read or write the developer's own "
        f"files — see tests/_fs_sandbox.py. Machine state belongs in the "
        f"throwaway dir redirect_local_state() sets up (or a module's own "
        f"MachineSettingsIsolation); a fixture belongs under tempfile.mkdtemp()."
    )


#: The `GIT_*` variables that re-point git at a repository regardless of where
#: the command was told to run. `GIT_COMMON_DIR` belongs with them because it
#: is where `config` — the file #482 wrote into — actually lives.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

#: Under the shared metadata, each linked worktree keeps its own `HEAD`, index
#: and refs in `worktrees/<name>`. Lower-case already, like the keys it extends.
_WORKTREES = "worktrees" + os.sep


def _same_repository(candidate: str, dirs: _RepoDirs) -> bool:
    """Whether ``candidate`` — a prefix key — belongs to the repository
    ``dirs`` describes.

    The shared metadata counts: `config` lives there, and `git -C <worktree>
    config` writes it whichever worktree asked. Another worktree's *private*
    gitdir never counts, and that is the case worth spelling out, because it
    sits under that same shared metadata and so reads as agreement to a plain
    prefix test. git takes `HEAD`, the index and the refs from `GIT_DIR`, so
    `git -C <worktree A> commit` with worktree B's gitdir in the environment
    commits A's files onto B's branch — the #482 damage, aimed at a sibling of
    the tree the work is in. Every change in this repository is made in its own
    worktree, so those siblings are the normal state here, not a rare layout.
    """
    siblings = dirs.common + _WORKTREES
    if candidate.startswith(siblings):
        return dirs.private.startswith(siblings) and candidate.startswith(dirs.private)
    return candidate.startswith((dirs.tree, dirs.private, dirs.common))


#: git's own global options that take their value as a *separate* argument.
#: `-C` is walked rather than skipped, so it is not among them; the rest have
#: to be consumed together with their value, because a value that does not
#: start with `-` otherwise reads as the subcommand. `--exec-path` is here for
#: the spelling that takes a path; bare, it prints one and exits, so consuming
#: the word after it costs nothing — that call reaches no repository.
_GIT_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "--attr-source",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--shallow-file",
        "--super-prefix",
        "--work-tree",
    }
)

#: git's own global options that take no value, as `git -h` lists them. With
#: the set above this is git's whole option surface before the subcommand, and
#: an option in neither is the one case the walk below refuses to guess at.
_GIT_FLAG_OPTIONS = frozenset(
    {
        "-P",
        "-h",
        "-p",
        "-v",
        "--bare",
        "--glob-pathspecs",
        "--help",
        "--html-path",
        "--icase-pathspecs",
        "--info-path",
        "--literal-pathspecs",
        "--man-path",
        "--no-advice",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "--noglob-pathspecs",
        "--paginate",
        "--version",
    }
)


class _GitMainOptions(NamedTuple):
    """What git's own options — the ones before the subcommand — say.

    ``target`` is where the `-C` options land, or None when the call passes
    none. ``unreadable`` names an option in neither table, which is also a
    statement that ``target`` cannot be trusted: if that option takes its value
    as a separate word, the walk ended on the value instead of on the
    subcommand, and any later `-C` went unseen.
    """

    target: str | None
    unreadable: str | None


def _git_main_options(argv: list[str], cwd: str) -> _GitMainOptions:
    """Read git's own options off ``argv``.

    Each `-C` is relative to the one before it, which is why this walks them
    rather than taking the last. The walk stops at the subcommand, because a
    `-C` after that belongs to the subcommand and means something else
    entirely — `git commit -C <commit>` reuses that commit's message.

    A global option whose value is a separate argument has to be consumed with
    that value. `git -c core.quotePath=false -C <tmp> config …` is an idiom
    this repository already writes (`scripts/lint_comments.py`,
    `test_prose_gate.py`), and reading `core.quotePath=false` as the subcommand
    ends the walk before the `-C` — which reports no target at all and lets
    exactly the call this guards against through unchecked.

    An option in *neither* table is reported rather than skipped, because
    skipping is that same failure for every name the tables do not list yet:
    `--attr-source <tree>` and `--shallow-file <path>` both take a separate
    value, and until they were added here they let the write through in
    silence. Reporting turns the next such option into a named test failure
    instead. A `--opt=value` spelling carries its value and needs no table, and
    an attached short spelling needs none either: git rejects both `-C=<path>`
    and `-C<path>` as unknown options, without reaching a repository.
    """
    here, seen = cwd, False
    rest = argv[1:]
    while rest:
        word = rest[0]
        if word == "--":
            break
        if word == "-C" and len(rest) > 1:
            here, seen, rest = os.path.join(here, rest[1]), True, rest[2:]
            continue
        if not word.startswith("-"):
            break
        if "=" in word or word in _GIT_FLAG_OPTIONS:
            rest = rest[1:]
            continue
        if word in _GIT_VALUE_OPTIONS:
            rest = rest[2:]
            continue
        return _GitMainOptions(here if seen else None, word)
    return _GitMainOptions(here if seen else None, None)


def git_env_conflict(argv: list[str], cwd: str, env: dict[str, str]) -> str | None:
    """Why this `git` call is ambiguous about which repository it means, or None.

    **`GIT_DIR` outranks `-C`.** A fixture that builds a scratch repo and calls
    `git -C <tmp> config user.name Test` looks self-contained and is not: when
    the suite runs under the pre-commit hook, `git commit` exports `GIT_DIR` to
    its hooks, so every one of those `config` writes landed in the real
    checkout's `.git/config` instead. That is how `user.name = Test` came to
    author 17 commits across four branches.

    The rule is disagreement, not presence. A call that names no `-C` is taking
    the ambient environment on purpose, and a call whose `-C` resolves to the
    same repository the `GIT_*` variables name wants that repository either way
    — `git -C <checkout> ls-files` under the pre-commit hook is the common case
    and is fine. What is always a bug is the two naming different repositories,
    or different worktrees of one: one of them is what the author meant and the
    other is what git will do.
    """
    named = [name for name in _GIT_LOCATION_ENV if env.get(name)]
    if not named:
        return None
    main = _git_main_options(argv, cwd)
    if main.unreadable is not None:
        return (
            f"test ran `git {main.unreadable} …` with {named[0]} in the environment, "
            f"and this guard cannot tell whether {main.unreadable} takes the next "
            f"word as its value — so it cannot say which repository the call's `-C` "
            f"names, while {named[0]} outranks -C either way. Strip the "
            f"ambient GIT_* from the environment you hand the subprocess (see "
            f"tests/_fakes.py), or list the option in _GIT_VALUE_OPTIONS or "
            f"_GIT_FLAG_OPTIONS in tests/_fs_sandbox.py."
        )
    if main.target is None:
        return None
    target = _resolve(os.path.join(cwd, main.target))
    dirs = _repo_dirs(target)
    for name in named:
        raw = env[name]
        # Resolved against the `-C` target rather than against `cwd`: git has
        # already changed directory by the time it reads these, so a relative
        # value names the repository the call asked for. `git commit` exports
        # GIT_INDEX_FILE=.git/index outside a worktree, and reading that as
        # cwd-relative refused `git -C <tmp> add` over a trap that was not there.
        if _same_repository(_key(_resolve(os.path.join(target, raw))), dirs):
            continue
        return (
            f"test ran `git -C {main.target!r}` with {name}={raw!r} in the environment. "
            f"{name} outranks -C, so this command acts on the repository {name} "
            f"names and not the one it was told to run in — which is how a fixture "
            f"wrote its test identity into a real checkout's .git/config. Strip the "
            f"ambient GIT_* from the environment you hand the subprocess (see "
            f"tests/_fakes.py), or drop the -C and mean the ambient repository."
        )
    return None


def _check_subprocess(args: tuple[object, ...]) -> None:
    if len(args) < 4:
        return
    executable, argv, cwd, env = args[0], args[1], args[2], args[3]
    if not isinstance(argv, (list, tuple)) or not argv:
        # Windows flattens the command line before raising the event, so there
        # is no argv to read a `-C` out of; POSIX always builds a list, and a
        # `shell=True` call arrives there as `/bin/sh -c …`. Both are in the
        # module docstring's blind spots.
        return
    if not all(isinstance(a, (str, bytes, os.PathLike)) for a in argv):
        return
    words = [os.fsdecode(a) for a in argv]
    if isinstance(executable, (str, bytes, os.PathLike)):
        program = os.fsdecode(executable)
    else:
        program = words[0]
    if os.path.basename(program) not in ("git", "git.exe"):
        return
    here = os.fsdecode(cwd) if isinstance(cwd, (str, bytes, os.PathLike)) else os.getcwd()
    # `env=None` means the child inherits ours, which is exactly the case that
    # bit: nothing in the fixture mentioned GIT_DIR because nothing had to.
    environ = dict(env) if isinstance(env, dict) else dict(os.environ)  # type: ignore[arg-type]
    complaint = git_env_conflict(words, here, environ)
    if complaint is not None:
        raise SandboxViolation(complaint)


def _hook(event: str, args: tuple[object, ...]) -> None:
    if not _armed or not args or getattr(_probing, "active", False):
        return
    if event == "subprocess.Popen":
        _check_subprocess(args)
        return
    if event not in _PATH_EVENTS:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return  # an open() on an already-open file descriptor
    raw = os.fsdecode(target)
    if not os.path.isabs(raw) and not os.path.dirname(raw):
        # A bare name, as `shutil.rmtree` and `TemporaryDirectory.cleanup` emit
        # for every entry of their fd-relative descent: the directory lives in
        # a file descriptor the audit event does not carry, so resolving
        # against cwd would blame the checkout for a temp-dir deletion. Nothing
        # this guards is reachable by a bare name: the machine paths and the
        # assets are all several components deep.
        return
    complaint = violation(raw)
    if complaint is not None:
        raise SandboxViolation(complaint)


def redirect_local_state() -> None:
    """Point the overridable machine-state paths at a throwaway directory for
    this process, so the machine layer reads as absent and nothing a writer
    creates lands in the real data dir.

    Runs once per process tree: a forked `unittest_parallel` worker inherits
    the marker along with the directory and leaves it alone. Keyed on a private
    marker rather than on the public overrides themselves, because those are
    documented user settings — a developer who exports `$C64CAST_DATA_DIR` to
    run c64cast for real would otherwise have had the suite honor it and write
    their actual calibrations and loop presets, reporting green the whole time.

    `$C64CAST_SETTINGS` names a file that does not exist, because the settings
    file is *read* and "absent" is the state a defaults test wants;
    `$C64CAST_DATA_DIR` is a real empty directory, because the data dir is
    *written* and its writers create what they need under it.
    """
    if _TAKEOVER_ENV in os.environ:
        return
    root = tempfile.mkdtemp(prefix="c64cast-suite-")
    owner = os.getpid()
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    os.environ[_TAKEOVER_ENV] = root
    os.environ[_SETTINGS_ENV] = os.path.join(root, "no-such-settings.toml")
    os.environ[_DATA_DIR_ENV] = data

    def cleanup() -> None:
        # A forked worker inherits this handler; without the owner check the
        # first to exit pulls the machine layer out from under the rest.
        if os.getpid() == owner:
            shutil.rmtree(root, ignore_errors=True)

    atexit.register(cleanup)


# Somewhere no machine has a character ROM. `char_rom` only calls
# `Path(...).is_file()` on it, so it is never opened and never audited.
_NO_CHARGEN = "/nonexistent/c64cast-suite-chargen.bin"


class _ChargenNeutralizer:
    """Import hook that blanks `LEGACY_CHARGEN_PATH` as `char_rom` is loaded.

    A finder that claims exactly one module, defers to the real machinery for
    the spec, and wraps the loader so the constant is rewritten the instant the
    module body has run — before anything can read it.
    """

    TARGET = "c64cast.hw.char_rom"

    def __init__(self) -> None:
        self._busy = False

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        if fullname != self.TARGET or self._busy:
            return None
        # A re-entrancy flag, not `sys.meta_path.remove(self)`: asking the
        # normal machinery for this spec re-enters us, and uninstalling to
        # break that disarms the hook for good — a spec can be looked up
        # without being executed, as `coverage run --source=<module>` does.
        self._busy = True
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            self._busy = False
        if spec is None or spec.loader is None:
            return None
        inner = spec.loader.exec_module

        def exec_module(module):
            inner(module)
            module.LEGACY_CHARGEN_PATH = _NO_CHARGEN

        spec.loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def neutralize_local_chargen() -> None:
    """Stop `char_rom.resolve` from finding a locally dumped character ROM.

    Its last fallback is `LEGACY_CHARGEN_PATH`, a *cwd-relative* path into
    `assets/roms/`, and the suite runs from the checkout — so on a machine that
    has dumped a ROM there, every scene that draws glyphs rendered real ones,
    while CI rendered the cv2 fallback. Neither run was wrong; they were
    testing different code, and no test said which. 233 reads on this
    machine, none on any other.

    Patched rather than redirected, because the path is a module constant with
    no environment override — `test_char_rom` still overrides it per test with
    `mock.patch.object`, which restores to this instead of to the real one.

    Deferred to an import hook rather than done by importing the module here:
    `sitecustomize` runs at interpreter startup, before coverage.py's tracer
    exists, so importing `c64cast.hw.char_rom` from it put that module and the
    packages above it into `sys.modules` unmeasured. The file then reported a
    fraction of the statements it demonstrably runs on every coverage run, with
    nothing in the output pointing at why. Nothing is lost by waiting: the
    patch lands as part of the import that first makes `resolve` reachable.
    """
    sys.meta_path.insert(0, _ChargenNeutralizer())


def arm() -> None:
    """Install the hook. Idempotent, and safe to call before the suite starts:
    an audit hook is permanent, so this flag — not the hook's presence — is
    what decides whether it polices anything."""
    global _armed
    if not _armed:
        sys.addaudithook(_hook)
        _armed = True


@contextlib.contextmanager
def allow_outside_checkout(path: str) -> Iterator[None]:
    """Exempt `path`, and anything under it, for the block.

    For the handful of tests whose subject *is* a real path — reading back what
    `--save-settings` would write, say. Prefer redirecting the path over
    widening the sandbox; this exists so a legitimate case doesn't have to
    fight the guard.

    Takes the path rather than disarming: the flag is process-wide and read by
    every thread, so suspending it also un-policed whatever the code under test
    had running in the background (a PollThread, the recorder) for as long as
    the block lasted, and nothing would have attributed a leak from one.
    """
    global _exempt
    was = _exempt
    _exempt = was + (_key(_resolve(path)),)
    try:
        yield
    finally:
        _exempt = was
