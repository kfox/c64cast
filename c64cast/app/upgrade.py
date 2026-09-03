"""One-command upgrade + update check, regardless of install method.

`--upgrade` and `--check-for-updates` both start from the question
`--version` already answers for a human: which install owns the `c64cast` on
PATH (see `cli._version_text`, added in response to a user who unpacked a
release archive over their working directory and reported the version not
moving — recognizing `uv/tools/` or `pipx/venvs/` in the printed path was
still a step *they* had to take). Detecting the installer here — genuinely
new: nothing else in the package parses that shape, it only ever expects a
reader to — is what lets one command do the right thing for `uv tool`, pipx,
a plain pip venv, a `uvx` throwaway run, and a development checkout alike.

Extras preservation needs no code of its own. `uv tool upgrade` and `pipx
upgrade` both replay their own recorded install spec (uv's
`uv-receipt.toml`, pipx's `pipx_metadata.json`), so a narrow install (e.g.
`c64cast[video,midi,web]`) keeps its extras across an upgrade without this
module reading either file.

Deliberately import-light at module scope — stdlib only; `requests` is
imported lazily inside :func:`latest_release`, matching the lazy-import
convention `cli_commands.py`'s introspection commands use for anything not
needed on every invocation. This keeps `--upgrade` usable even when a hard
runtime dependency (cv2, numpy, ...) is broken, which is exactly the
situation an upgrade exists to fix.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard

log = logging.getLogger(__name__)

InstallKind = Literal["uv-tool", "pipx", "pip", "uvx", "checkout", "unknown"]

UPGRADE_TIMEOUT_ENV = "C64CAST_UPGRADE_TIMEOUT_S"
"""Overrides :data:`_UPGRADE_TIMEOUT_S`; `0` removes the ceiling entirely."""

_UPGRADE_TIMEOUT_S = 3600.0
"""How long one install command may run before `--upgrade` stops it.

An hour, not the two minutes this used to be. The commands under it
(`uv sync --all-extras`, `pipx upgrade`, `pip install --upgrade`, `git pull`)
mutate the environment, and a release that moves an `opencv-python`/PyAV/numpy
pin downloads ~100 MB — or builds it from source on a Pi-class host with no
matching wheel, which two minutes does not cover. Stopping one of those
partway through replacing site-packages is how the command that exists to
*repair* an install leaves a broken one, so the ceiling is set where only a
genuinely wedged process reaches it, and `$C64CAST_UPGRADE_TIMEOUT_S` lifts
it for a slow link. It is not removed outright because `--yes` makes
unattended runs (the appliance, CI) legitimate, and those have no user at the
console to notice a hang."""

_INTERRUPT_GRACE_S = 20.0
"""How long an install command gets to unwind after :func:`_interrupt` before
it is killed outright."""

_PROBE_TIMEOUT_S = 10.0
"""Ceiling on the read-only `git status` probe, which mutates nothing and so
is safe to kill."""

PYPI_URL = "https://pypi.org/pypi/c64cast/json"

_GUIDE_UPGRADE_URL = "https://kfox.github.io/c64cast/guide/04-setting-up.html#upgrading"

_VERSION_TOKEN_RE = re.compile(r"[A-Za-z0-9._+!-]{1,64}")


def install_root() -> Path:
    """The directory the running `c64cast` package sits in: site-packages for
    an installed distribution, the repo root for a source checkout.

    The single home for an expression `cli.py` and `doctor.py` each used to
    compute independently (`Path(__file__).resolve().parents[2]`). Any module
    living directly in `c64cast/app/` gets the same answer regardless of
    whose `__file__` supplies it, so consolidating here removes a drift risk
    rather than adding a third copy.
    """
    return Path(__file__).resolve().parents[2]


def running_from_checkout(*, root: Path | None = None) -> bool:
    """True when running out of this package's own source tree — a sibling
    `pyproject.toml`, which an installed distribution's root (site-packages)
    never has. `root` is injectable for tests; real callers let it default to
    :func:`install_root`."""
    root = root if root is not None else install_root()
    return (root / "pyproject.toml").is_file()


@dataclass(frozen=True)
class Install:
    """One installation's shape: what kind it is, where it lives, and the
    argv that upgrades it (None when there isn't a single command — `uvx`
    has nothing installed to upgrade, `unknown` has no known one)."""

    kind: InstallKind
    root: Path
    command: list[str] | None


def looks_like_version(value: object) -> TypeGuard[str]:
    """True for a string shaped like a version somebody could publish — PEP
    440 permits only these characters — and False for everything else, `None`
    and a non-string included.

    Two trust boundaries share it. :func:`latest_release` would otherwise
    `str()` a mis-shaped `info.version` into a confident answer:
    `{"info": {"version": 5}}` reads as `"5"`, which compares *newer* than
    every release this project has published. And `update_state` needs it
    because both versions it records are interpolated into a line
    `/etc/update-motd.d/` prints as root — a newline in one forges an extra
    MOTD line, and an ESC byte rewrites the banner around it."""
    return isinstance(value, str) and _VERSION_TOKEN_RE.fullmatch(value) is not None


def _has_adjacent(parts: tuple[str, ...], pair: tuple[str, str]) -> bool:
    """True when `pair` appears as *consecutive* path components — the
    `uv/tools/` and `pipx/venvs/` shapes :func:`detect_install` names.

    Unordered membership (`"uv" in parts and "tools" in parts`) also matched
    a hand-made pip venv that merely happens to sit under a directory called
    `tools` beside one called `uv`, and printed that user the wrong
    installer's command."""
    return any(parts[i : i + 2] == pair for i in range(len(parts) - 1))


def detect_install(*, root: Path | None = None) -> Install:
    """Identify which installer owns the running `c64cast`, from the shape
    of its install root alone.

    No receipt file is parsed — deliberately: both recommended installers
    replay their own recorded extras on upgrade (see the module docstring),
    so nothing here needs to know what was installed, only how to ask that
    installer to redo it. `root` is injectable for tests; real callers
    always let it default to :func:`install_root`.

    Checked in this order:

    1. **checkout** — a sibling `pyproject.toml`, checked first because nothing
       below should ever override "this is c64cast's own source tree".
    2. **uvx** — a path through uv's versioned cache (`archive-v0`), which only
       ever holds ephemeral `uvx`/`uv run --with` environments; a `uv tool
       install` environment lives under `uv/tools/` instead, never here.
    3. **uv-tool** — `uv/tools/` as adjacent components of the path.
    4. **pipx** — `pipx/venvs/`, likewise adjacent.
    5. **pip** — any other `site-packages`, e.g. a hand-made venv.
    6. **unknown** — none of the above; `--upgrade` can only name the path and
       point at the guide.
    """
    root = root if root is not None else install_root()
    parts = root.parts

    if running_from_checkout(root=root):
        return Install("checkout", root, None)
    if "archive-v0" in parts:
        return Install("uvx", root, None)
    if _has_adjacent(parts, ("uv", "tools")):
        return Install("uv-tool", root, ["uv", "tool", "upgrade", "c64cast"])
    if _has_adjacent(parts, ("pipx", "venvs")):
        return Install("pipx", root, ["pipx", "upgrade", "c64cast"])
    if "site-packages" in parts:
        return Install(
            "pip", root, [sys.executable, "-m", "pip", "install", "--upgrade", "c64cast"]
        )
    return Install("unknown", root, None)


def latest_release(*, timeout: float = 5.0) -> str | None:
    """The current version on PyPI, or None on any failure — network down, a
    non-2xx response, a body that doesn't parse the way expected, or a
    `requests` too broken to import. Never raises: the caller always has a
    "couldn't check" fallback to print, and `doctor._probe_updates` is
    specified never to report `error`.

    That promise is kept by catching broadly rather than by naming exception
    types, which had been wrong twice: a body decoding to a list or to
    `{"info": null}` raises `TypeError` from the subscript, and the lazy
    `import requests` sat outside the guard, so a half-installed `requests`
    — the state an upgrade exists to fix — raised `ImportError` straight
    through. The caught exception is logged at debug, so `-vv` can still tell
    a DNS failure from a proxy's 403 from a change in the shape of
    `info.version`; without it, every one of those was the same
    indistinguishable None and the only trace on an appliance was an
    `unanswered_since` timestamp.

    The version itself is type-checked (:func:`looks_like_version`) rather
    than `str()`-coerced: coercion turned a well-shaped body with a wrong
    field type into a *fabricated* answer that nothing downstream could
    catch, `{"info": {"version": 5}}` reading as the release `"5"`.

    Mirrors the house idiom for a one-shot outbound fetch
    (`scenes/overlays/rss.py`'s `_fetch_once`): a `User-Agent` naming this
    package, an explicit timeout, `raise_for_status()`, and the failure
    modes caught rather than propagated.
    """
    try:
        import requests

        from c64cast import __version__

        r = requests.get(
            PYPI_URL, timeout=timeout, headers={"User-Agent": f"c64cast/{__version__}"}
        )
        r.raise_for_status()
        body = r.json()
        info = body.get("info") if isinstance(body, dict) else None
        version = info.get("version") if isinstance(info, dict) else None
    except Exception as e:
        log.debug("PyPI update check failed: %s", e)
        return None

    if not looks_like_version(version):
        log.debug("PyPI answered with no usable version: %r", version)
        return None
    return version


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """The leading numeric release segment of a version string, e.g.
    `"0.4.0rc1"` -> `(0, 4, 0)`, `"bogus"` -> `None`.

    The uninstalled sentinel `c64cast.UNINSTALLED_VERSION` ("0+unknown")
    parses here too, as `(0,)` — which is why :func:`is_newer` rejects it by
    *value* before it ever reaches this parser, rather than relying on the
    parser to refuse it. Do not read that guard as redundant: without it
    every published release reads as "newer" than "not installed".

    Stops at the first non-digit character in each dot-separated part —
    covering the `a`/`b`/`rc`/`.dev`/`+local` suffixes
    `scripts/bump_version.py`'s `VERSION_RE` permits — rather than raising.
    `packaging` would be the precise PEP 440 comparator, but it isn't a
    runtime dependency here, and comparing on release numbers alone is exact
    for every version this project actually publishes.
    """
    release: list[int] = []
    for part in version.split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        release.append(int(digits))
    return tuple(release) if release else None


def is_newer(remote: str, local: str) -> bool | None:
    """True if `remote` is a newer release than `local`, False if not, None
    if either couldn't be parsed (report "couldn't compare", never guess).

    `local == c64cast.UNINSTALLED_VERSION` ("0+unknown") is checked
    explicitly rather than left to `_release_tuple`: the sentinel's leading
    "0" parses as a syntactically valid release segment, which would make
    every real release look "newer" than "not installed" — true in spirit,
    but not the same claim as "here is a newer release of what you have".

    PyPI's `info.version` never reports a prerelease, so a tied release
    segment (`local` = `"0.4.0rc1"`, `remote` = `"0.4.0"`) means the remote
    stable release has already superseded whatever prerelease is running —
    hence the fallback string comparison on a tie, rather than treating it
    as "equal, not newer".
    """
    from c64cast import UNINSTALLED_VERSION

    if local == UNINSTALLED_VERSION:
        return None
    remote_release = _release_tuple(remote)
    local_release = _release_tuple(local)
    if remote_release is None or local_release is None:
        return None
    if remote_release != local_release:
        return remote_release > local_release
    return local != remote


def _checkout_is_dirty(root: Path) -> bool | None:
    """True/False when `git status --porcelain` answers cleanly, None when it
    couldn't be run at all (git missing, or the call itself failed) — treated
    the same as "dirty" by the caller, since "can't verify" is not "clean"."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    """Ask before an install-mutating action, unless overridden.

    Same idiom as the live-tune save prompt in `session.py`: check
    `sys.stdin.isatty()` before reading, so a script or CI run without
    `--yes` fails fast with an actionable message instead of hanging on a
    read that will never come."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{prompt} — refusing without a terminal. Pass --yes to proceed.", file=sys.stderr)
        return False
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return ans in ("y", "yes")


def _upgrade_timeout_s() -> float | None:
    """The ceiling on one install command, or None for "wait indefinitely".

    `$C64CAST_UPGRADE_TIMEOUT_S` overrides :data:`_UPGRADE_TIMEOUT_S`, and
    `0` (or anything negative) removes the ceiling — the escape hatch for a
    slow link or a host building wheels from source, where the alternative is
    running the installer by hand. An unparsable value is reported and
    ignored rather than treated as `0`, since silently removing the ceiling
    is the one reading a typo must not get."""
    raw = os.environ.get(UPGRADE_TIMEOUT_ENV)
    if raw is None:
        return _UPGRADE_TIMEOUT_S
    try:
        seconds = float(raw)
    except ValueError:
        print(f"c64cast: ignoring {UPGRADE_TIMEOUT_ENV}={raw!r} — not a number.", file=sys.stderr)
        return _UPGRADE_TIMEOUT_S
    return seconds if seconds > 0 else None


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Get an install command gone, as gently as it allows.

    SIGINT first — the signal uv/pip/pipx/git already unwind cleanly from,
    and the one an impatient user at the console would have sent. It matters
    which: `subprocess.run(timeout=...)` goes straight to SIGKILL, and
    SIGKILL partway through replacing site-packages is how the one command
    meant to *repair* an install leaves a broken one. A child that ignores
    the interrupt gets `_INTERRUPT_GRACE_S` and then SIGKILL, so this never
    waits indefinitely. Windows has no per-child equivalent of SIGINT, so it
    gets `terminate()`."""
    if os.name == "posix":
        process.send_signal(signal.SIGINT)
    else:
        process.terminate()
    try:
        process.wait(timeout=_INTERRUPT_GRACE_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_out(process: subprocess.Popen[bytes], timeout: float | None) -> int | None:
    """`process`'s exit status, or None when it outlived `timeout` and had to
    be stopped (:func:`_stop`).

    A Ctrl-C already reaches the child through the foreground process group,
    but it is stopped here too: `subprocess.run` used to kill the child on
    any exception, and without that a child which shrugs off the interrupt
    would be waited on forever by `Popen.__exit__`."""
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process)
        return None
    except BaseException:
        _stop(process)
        raise


def _run_command(command: list[str], *, cwd: Path | None = None) -> int:
    """Run an upgrade command with the console attached — unlike
    `doctor.py`'s probes, the point is for the user to see uv/pipx/pip/git's
    own progress output, not a captured summary. A missing binary, a failed
    spawn, or a process that outlives the ceiling all degrade to a message +
    exit 3, never a traceback.

    The binary is launched by the path `shutil.which` resolved, not by the
    name it was looked up under: `exec` would otherwise re-resolve PATH, so
    the executable that was verified to exist need not be the one that runs
    — which matters most under the `sudo -E` an admin reaches for on a system
    install."""
    exe = shutil.which(command[0])
    if exe is None:
        print(
            f"c64cast: '{command[0]}' is not on PATH — cannot run: {' '.join(command)}",
            file=sys.stderr,
        )
        return 3
    timeout = _upgrade_timeout_s()
    try:
        with subprocess.Popen([exe, *command[1:]], cwd=cwd) as process:
            rc = _wait_out(process, timeout)
    except OSError as e:
        print(f"c64cast: {' '.join(command)} failed: {e}", file=sys.stderr)
        return 3
    if rc is None:
        print(
            f"c64cast: {' '.join(command)} was still running after {timeout} s "
            "and has been stopped. It may have applied only part of the "
            "upgrade — re-run it by hand to finish. If the install is simply "
            f"slow, raise ${UPGRADE_TIMEOUT_ENV} (0 for no limit) first.",
            file=sys.stderr,
        )
        return 3
    return rc


def _refuse(message: str) -> int:
    """Say why `--upgrade` will not act, and give it its refusal exit code."""
    print(f"c64cast: {message}", file=sys.stderr)
    return 2


def _upgrade_checkout(root: Path, *, assume_yes: bool) -> int:
    """`--upgrade` for a development checkout: `git pull`, then `uv sync
    --all-extras`. Four refusals come before either command runs.

    **No `.git`** means an unpacked release archive rather than a checkout —
    `pyproject.toml` ships inside the sdist, so the user this module's
    docstring is written for (the one who unpacked an archive over their
    working directory) lands in this branch with no repository at all.
    `git status` exits 128 there, which reads as "could not be checked" and
    used to send that user off to fix a PATH that was never the problem and
    stash changes in a directory holding no repository.

    A **dirty tree** is the deliberate deviation from "always run the two
    commands": a pull colliding with in-progress work is exactly the kind of
    surprise an upgrade must not spring. **"Can't verify"** is refused the
    same way — not "clean" — and says so in its own words rather than
    borrowing the dirty tree's advice to commit or stash.

    A missing **uv** is checked up front because `_run_command`'s own check
    is per-command, so it would otherwise be discovered *after* `git pull`
    had already moved the source: the tree left on new code with the old
    dependency set, plausibly unimportable if the release moved an
    opencv/PyAV/numpy pin. A checkout driven from a plain pip venv with no uv
    on PATH is a legitimate shape.
    """
    if not (root / ".git").exists():
        return _refuse(
            f"{root} has a pyproject.toml but no git repository — an unpacked "
            "source archive rather than a checkout, which --upgrade cannot "
            f"pull into. Install a release instead: {_GUIDE_UPGRADE_URL}"
        )
    dirty = _checkout_is_dirty(root)
    if dirty is None:
        return _refuse(
            f"could not check whether {root} is clean (is git on PATH?) — "
            "refusing to `git pull` over it."
        )
    if dirty:
        return _refuse(
            f"{root} has uncommitted changes — refusing to `git pull` over "
            "it. Commit or stash first, then re-run --upgrade."
        )
    if shutil.which("uv") is None:
        return _refuse(
            "'uv' is not on PATH — refusing to `git pull` a checkout that "
            "could not then be synced. Install uv, then re-run --upgrade."
        )

    print(f"Development checkout at {root}. Upgrading with:")
    print("  git pull")
    print("  uv sync --all-extras")
    if not _confirm("Proceed?", assume_yes=assume_yes):
        return 2
    rc = _run_command(["git", "pull"], cwd=root)
    if rc != 0:
        return rc
    return _run_command(["uv", "sync", "--all-extras"], cwd=root)


def run_upgrade(*, assume_yes: bool = False) -> int:
    """`--upgrade`: detect the install, print the exact command, confirm,
    run it. Exit 0 on success (including the uvx no-op); 2 for a refusal
    (unknown install, an unpacked archive, a dirty or unverifiable checkout,
    a missing `uv`, a declined or no-TTY confirmation); the installer's own
    return code otherwise.

    Every branch dispatches on `install.kind`. Two kinds carry no command
    today (`uvx`, `unknown`), so the older `install.command is None` test was
    correct only because of where it sat in the chain — and a sixth
    command-less kind (a distro package, a read-only appliance install) would
    have silently impersonated `unknown`. It now names the kind it could not
    route instead."""
    install = detect_install()

    if install.kind == "uvx":
        print(
            "c64cast is running from a uvx throwaway invocation — there is "
            "nothing installed to upgrade. The next `uvx --from "
            "'c64cast[...]' c64cast ...` already fetches the latest release."
        )
        return 0

    if install.kind == "checkout":
        return _upgrade_checkout(install.root, assume_yes=assume_yes)

    if install.kind == "unknown":
        return _refuse(
            f"could not tell how this install ({install.root}) was made. See "
            "the Upgrading section of the User's Guide for the command that "
            f"matches your install method: {_GUIDE_UPGRADE_URL}"
        )

    if install.command is None:
        return _refuse(
            f"no upgrade command is known for a {install.kind} install "
            f"({install.root}). See {_GUIDE_UPGRADE_URL}"
        )

    print(f"Upgrading via {install.kind} — running:")
    print(f"  {' '.join(install.command)}")
    if not _confirm("Proceed?", assume_yes=assume_yes):
        return 2
    return _run_command(install.command)
