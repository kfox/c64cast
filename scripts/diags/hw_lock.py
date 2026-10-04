#!/usr/bin/env python3
"""Run a command while holding an exclusive lock on one piece of hardware.

The U64's DMA service takes one connection at a time and a capture device has
one reader, so two agents or scripts driving the same rig at once break each
other's runs. Prefix every command that touches the rig with this tool and they
take turns instead:

    scripts/diags/hw_lock.py scripts/diags/u64_probe.py --reset
    scripts/diags/hw_lock.py uv run python -m c64cast -u u64://192.168.2.64
    scripts/diags/hw_lock.py --device u64://192.168.2.65 uv run scripts/diags/hdmi_capture.py

It blocks until the lock is free, saying on stderr who holds it and how long
the wait took, then *becomes* the command (exec), so the command's exit code,
signals and terminal are its own and Ctrl-C reaches it directly. The command
inherits the lock's descriptor, and the lock is held until every process that
still has that descriptor open has exited; a child started with its descriptors
closed (Python's ``subprocess`` default) does not hold it. The kernel drops the
lock when the holder dies, so a killed run leaves no stale lock behind.

A command run under the lock may itself call this tool for the same device: the
lock is already its own, so that inner call runs its command without waiting.

``--device`` picks which lock: a URL keys on its host, so ``u64://HOST`` and
``http://HOST`` share one, and anything else is used as given. Every command on
a one-rig machine can leave it at its default; a second rig gets its own key.

Locks live under ``$C64_DIAG_LOCK_DIR``, default ``~/.cache/c64cast/locks``
(``$XDG_CACHE_HOME`` honored) — per user, shared by every checkout and worktree.
POSIX only: Windows has no lock that survives exec, so it exits with an error.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_DEVICE = "default"
HELD_ENV = "C64_DIAG_LOCK_HELD"


def lock_dir() -> Path:
    override = os.environ.get("C64_DIAG_LOCK_DIR")
    if override:
        return Path(override)
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "c64cast" / "locks"


def lock_key(device: str) -> str:
    """The file-name-safe key for ``device``: a URL's host, else the string."""
    try:
        host = urlsplit(device).hostname if "://" in device else None
    except ValueError:
        host = None
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", (host or device).lower()).strip("_")
    return key or DEFAULT_DEVICE


def lock_path(device: str) -> Path:
    return lock_dir() / f"{lock_key(device)}.lock"


def _held_by_an_ancestor(path: Path) -> bool:
    return str(path) in os.environ.get(HELD_ENV, "").split(os.pathsep)


def _exec(command: list[str]) -> int:
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"hw_lock: cannot run {command[0]!r}: {exc}", file=sys.stderr)
    return 127


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"which hardware to lock: a URL (keys on its host) or a name (default {DEFAULT_DEVICE!r})",
    )
    ap.add_argument("command", nargs=argparse.REMAINDER, help="the command to run under the lock")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command given")
    if sys.platform == "win32":
        print(
            "hw_lock: POSIX only (needs flock); run the command directly on Windows",
            file=sys.stderr,
        )
        return 2

    import fcntl

    path = lock_path(args.device)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    started = time.monotonic()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if _held_by_an_ancestor(path):
            os.close(fd)
            return _exec(command)
        holder = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip() or "unknown"
        print(f"hw_lock: waiting for {path} (held by pid {holder})", file=sys.stderr, flush=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except KeyboardInterrupt:
            print("hw_lock: interrupted while waiting; command not run", file=sys.stderr)
            return 130
        print(
            f"hw_lock: acquired {path} after {time.monotonic() - started:.0f}s",
            file=sys.stderr,
            flush=True,
        )

    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}: {shlex.join(command)}\n".encode(), 0)
    os.set_inheritable(fd, True)
    if not _held_by_an_ancestor(path):
        os.environ[HELD_ENV] = os.pathsep.join(filter(None, [os.environ.get(HELD_ENV), str(path)]))
    return _exec(command)


if __name__ == "__main__":
    sys.exit(main())
