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

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

InstallKind = Literal["uv-tool", "pipx", "pip", "uvx", "checkout", "unknown"]

# Generous: unlike the read-only probes in doctor.py, these commands (uv/pipx/
# pip resolving a real upgrade, or `git pull`) may hit the network for actual
# package/ref resolution rather than a single fast check.
_SUBPROCESS_TIMEOUT_S = 120.0

PYPI_URL = "https://pypi.org/pypi/c64cast/json"


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
    3. **uv-tool** — `uv/tools/` in the path.
    4. **pipx** — `pipx/venvs/` in the path.
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
    if "uv" in parts and "tools" in parts:
        return Install("uv-tool", root, ["uv", "tool", "upgrade", "c64cast"])
    if "pipx" in parts and "venvs" in parts:
        return Install("pipx", root, ["pipx", "upgrade", "c64cast"])
    if "site-packages" in parts:
        return Install(
            "pip", root, [sys.executable, "-m", "pip", "install", "--upgrade", "c64cast"]
        )
    return Install("unknown", root, None)


def latest_release(*, timeout: float = 5.0) -> str | None:
    """The current version on PyPI, or None on any failure — network down,
    a non-2xx response, or a body that doesn't parse the way expected. Never
    raises: the caller always has a "couldn't check" fallback to print.

    Mirrors the house idiom for a one-shot outbound fetch
    (`scenes/overlays/rss.py`'s `_fetch_once`): a `User-Agent` naming this
    package, an explicit timeout, `raise_for_status()`, and the failure
    modes caught rather than propagated.
    """
    import requests

    from c64cast import __version__

    try:
        r = requests.get(
            PYPI_URL, timeout=timeout, headers={"User-Agent": f"c64cast/{__version__}"}
        )
        r.raise_for_status()
        return str(r.json()["info"]["version"])
    except (requests.RequestException, ValueError, KeyError):
        return None


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """The leading numeric release segment of a version string, e.g.
    `"0.4.0rc1"` -> `(0, 4, 0)`, `"0+unknown"` -> `None` (the uninstalled
    sentinel, `c64cast.UNINSTALLED_VERSION`, has no release segment).

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
            timeout=10,
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


def _run_command(command: list[str], *, cwd: Path | None = None) -> int:
    """Run an upgrade command with the console attached — unlike
    `doctor.py`'s probes, the point is for the user to see uv/pipx/pip/git's
    own progress output, not a captured summary. A missing binary or a hung
    process both degrade to a message + exit 3, never a traceback."""
    tool = command[0]
    if shutil.which(tool) is None:
        print(
            f"c64cast: '{tool}' is not on PATH — cannot run: {' '.join(command)}", file=sys.stderr
        )
        return 3
    try:
        result = subprocess.run(command, cwd=cwd, timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"c64cast: {' '.join(command)} failed: {e}", file=sys.stderr)
        return 3
    return result.returncode


def run_upgrade(*, assume_yes: bool = False) -> int:
    """`--upgrade`: detect the install, print the exact command, confirm,
    run it. Exit 0 on success (including the uvx no-op); 2 for a refusal
    (unknown install, dirty checkout, declined/no-TTY confirmation); the
    installer's own return code otherwise."""
    install = detect_install()

    if install.kind == "uvx":
        print(
            "c64cast is running from a uvx throwaway invocation — there is "
            "nothing installed to upgrade. The next `uvx --from "
            "'c64cast[...]' c64cast ...` already fetches the latest release."
        )
        return 0

    if install.kind == "checkout":
        dirty = _checkout_is_dirty(install.root)
        if dirty is not False:
            reason = (
                "has uncommitted changes" if dirty else "could not be checked (is git on PATH?)"
            )
            print(
                f"c64cast: {install.root} {reason} — refusing to `git pull` "
                "over it. Commit or stash first, then re-run --upgrade.",
                file=sys.stderr,
            )
            return 2
        print(f"Development checkout at {install.root}. Upgrading with:")
        print("  git pull")
        print("  uv sync --all-extras")
        if not _confirm("Proceed?", assume_yes=assume_yes):
            return 2
        rc = _run_command(["git", "pull"], cwd=install.root)
        if rc != 0:
            return rc
        return _run_command(["uv", "sync", "--all-extras"], cwd=install.root)

    if install.command is None:  # "unknown"
        print(
            f"c64cast: could not tell how this install ({install.root}) was "
            "made. See the Upgrading section of the User's Guide for the "
            "command that matches your install method: "
            "https://kfox.github.io/c64cast/guide/04-setting-up.html#upgrading",
            file=sys.stderr,
        )
        return 2

    print(f"Upgrading via {install.kind} — running:")
    print(f"  {' '.join(install.command)}")
    if not _confirm("Proceed?", assume_yes=assume_yes):
        return 2
    return _run_command(install.command)
