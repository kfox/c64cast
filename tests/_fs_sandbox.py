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
  `$HOME`.** `assets/` is carved back out of the checkout, since everything
  there but the READMEs and the logo is gitignored.

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

Two blind spots worth knowing:

* A violation is raised at the `open()` call site, so code that catches
  broadly can swallow it. `SandboxViolation` derives from `AssertionError` —
  reported as a plain test failure, and outside the `except OSError` most of
  the resolver chain uses — but an `except Exception` in front of a leak would
  hide it. The next unguarded access still reports.
* A path with no directory part goes unchecked, because it cannot be resolved
  here (see `_hook`).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator

_SETTINGS_ENV = "C64CAST_SETTINGS"
_DATA_DIR_ENV = "C64CAST_DATA_DIR"

# Filesystem audit events whose first argument is a path. Not the complete set
# CPython raises — the ones a test could plausibly reach a real file through.
# `open` alone covers every read and every rewrite; the rest catch the
# directory and metadata operations that would let a test create, move or
# delete something outside the checkout without opening it.
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


CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
_armed = False


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


def _hook(event: str, args: tuple[object, ...]) -> None:
    if not _armed or event not in _PATH_EVENTS or not args:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return  # an open() on an already-open file descriptor
    raw = os.fsdecode(target)
    if not os.path.isabs(raw) and not os.path.dirname(raw):
        # A bare name, which `shutil.rmtree` and `TemporaryDirectory.cleanup`
        # emit for every entry of their fd-relative descent. The directory it
        # is relative to lives in the file descriptor, which the audit event
        # does not carry, so resolving against cwd would blame the checkout for
        # a file deleted inside a temp dir. Nothing this guards is reachable by
        # a bare name: the machine paths and the assets are all several
        # components deep.
        return
    complaint = violation(raw)
    if complaint is not None:
        raise SandboxViolation(complaint)


def redirect_local_state() -> None:
    """Point the overridable machine-state paths at a throwaway directory for
    this process, so the machine layer reads as absent and nothing a writer
    creates lands in the real data dir.

    Leaves the environment alone if the caller already set it: that is how
    `MachineSettingsIsolation`, the tests that write a settings file of their
    own, and a forked worker inheriting this one all take over.

    `$C64CAST_SETTINGS` names a file that does not exist, because the settings
    file is *read* and "absent" is the state a defaults test wants;
    `$C64CAST_DATA_DIR` is a real empty directory, because the data dir is
    *written* and its writers create what they need under it.
    """
    if _SETTINGS_ENV in os.environ and _DATA_DIR_ENV in os.environ:
        return
    root = tempfile.mkdtemp(prefix="c64cast-suite-")
    owner = os.getpid()
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    os.environ.setdefault(_SETTINGS_ENV, os.path.join(root, "no-such-settings.toml"))
    os.environ.setdefault(_DATA_DIR_ENV, data)

    def cleanup() -> None:
        # A forked worker inherits this handler along with the directory, and
        # the first one to exit would otherwise pull the machine layer out from
        # under every worker still running.
        if os.getpid() == owner:
            shutil.rmtree(root, ignore_errors=True)

    atexit.register(cleanup)


# Somewhere no machine has a character ROM. `char_rom` only ever calls
# `Path(...).is_file()` on it, so it is never opened and never audited.
_NO_CHARGEN = "/nonexistent/c64cast-suite-chargen.bin"


def neutralize_local_chargen() -> None:
    """Stop `char_rom.resolve` from finding a locally dumped character ROM.

    Its last fallback is `LEGACY_CHARGEN_PATH`, a *cwd-relative* path into
    `assets/roms/`, and the suite runs from the checkout — so on a machine that
    has dumped a ROM there, every scene that draws glyphs rendered real ones,
    while CI rendered the cv2 fallback. Neither run was wrong; they were
    testing different code, and no test said which. 233 reads on this
    machine, none on any other.

    Patched here rather than redirected, because the path is a module constant
    with no environment override — `test_char_rom` still overrides it per test
    with `mock.patch.object`, which restores to this instead of to the real
    one. Costs one ~30 ms import of `c64cast.hw.char_rom` per worker process at
    startup, which is why it is its own call and not folded into
    `redirect_local_state`.
    """
    from c64cast.hw import char_rom

    char_rom.LEGACY_CHARGEN_PATH = _NO_CHARGEN


def arm() -> None:
    """Install the hook. Idempotent, and safe to call before the suite starts:
    an audit hook is permanent, so the `_armed` flag — not the hook's presence
    — is what `allow_outside_checkout` toggles."""
    global _armed
    if not _armed:
        sys.addaudithook(_hook)
        _armed = True


@contextlib.contextmanager
def allow_outside_checkout() -> Iterator[None]:
    """Suspend the sandbox for the block.

    For the handful of tests whose subject *is* a real path — reading back what
    `--save-settings` would write, say. Prefer redirecting the path over
    widening the sandbox; this exists so a legitimate case doesn't have to
    fight the guard.
    """
    global _armed
    was = _armed
    _armed = False
    try:
        yield
    finally:
        _armed = was
