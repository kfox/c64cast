#!/usr/bin/env python3
"""Run a command while holding an exclusive lock on one piece of hardware.

The U64's DMA service takes one connection at a time and a capture device has
one reader, so two agents or scripts driving the same rig at once break each
other's runs. Prefix every command that touches the rig with this tool and they
take turns instead:

    scripts/diags/hw_lock.py scripts/diags/u64_probe.py --reset
    scripts/diags/hw_lock.py uv run python -m c64cast -u u64://HOST
    scripts/diags/hw_lock.py --device u64://HOST uv run scripts/diags/hdmi_capture.py

It blocks until the lock is free, saying on stderr who holds it and how long
the wait took, then *becomes* the command (exec), so the command's exit code,
signals and terminal are its own and Ctrl-C reaches it directly. The command
inherits the lock's descriptor, and the lock is held until every process that
still has that descriptor open has exited; a child started with its descriptors
closed (Python's ``subprocess`` default) does not hold it. The kernel drops the
lock when the holder dies, so a killed run leaves no stale lock behind.

A command run under the lock may itself call this tool: when the enclosing call
already holds the rig lock the inner ``--device`` needs, the inner call runs its
command without waiting and takes nothing more, so it cannot deadlock. An inner
call that needs a rig lock the enclosing call does not hold (another rig's, under
``$C64_DIAG_RIGS``) is an error rather than a run beside that rig's holder.

``--device`` names what the command touches — a URL (keyed on its host, so
``u64://HOST`` and ``http://HOST`` are one device), a capture device's name, or
nothing. On its own it does not pick a separate lock: every invocation takes the
one rig lock, whatever ``--device`` says, because one machine usually has one
rig and its U64, capture device and audio input are only usable together. A
second rig gets its own lock only by opting in through ``$C64_DIAG_RIGS``,
which names each rig and the devices on it::

    C64_DIAG_RIGS="u64=u64://HOST1,CAPTURE-NAME-1;u2p=http://HOST2,CAPTURE-NAME-2"

A ``--device`` listed under a rig takes only that rig's lock, plus any lock file
already present under one of that rig's device keys. Anything else —
no ``--device``, or one the map does not list — takes every lock in the
directory, so a partial map makes the unlisted devices wait on every rig
rather than run beside one. A malformed map is an error, not a fallback.

Taking every lock file present also excludes a caller from before the one-rig
lock, which keyed a separate file on each ``--device`` spelling: its file
(``default.lock``, ``HOST.lock``, a capture device's name) is in the directory,
so the new tool waits for it. Several locks are always taken in sorted path
order, so two callers cannot each hold one the other is waiting on.

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
RIGS_ENV = "C64_DIAG_RIGS"


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


class RigMapError(ValueError):
    pass


def parse_rigs(spec: str) -> dict[str, str]:
    """Map each device key in ``$C64_DIAG_RIGS`` to its rig's key."""
    rigs: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in spec.split(";"))):
        name, sep, devices = entry.partition("=")
        listed = [d.strip() for d in devices.split(",") if d.strip()]
        if not sep or not name.strip() or not listed:
            raise RigMapError(f"{RIGS_ENV} entry {entry!r} is not NAME=DEVICE[,DEVICE…]")
        rig = lock_key(name.strip())
        for device in listed:
            key = lock_key(device)
            if rigs.setdefault(key, rig) != rig:
                raise RigMapError(f"{RIGS_ENV} puts {device!r} on two rigs")
    return rigs


def rig_locks(device: str | None, rigs: dict[str, str]) -> set[Path]:
    """The rig lock files ``device`` needs: its rig's, or every rig's when unmapped."""
    rig = rigs.get(lock_key(device)) if device else None
    names = {rig} if rig is not None else {DEFAULT_DEVICE, *rigs.values()}
    return {lock_dir() / f"{name}.lock" for name in names}


def lock_paths(device: str | None, rigs: dict[str, str] | None = None) -> list[Path]:
    """Every lock file ``device`` must hold, in the order they are taken."""
    if rigs is None:
        rigs = parse_rigs(os.environ.get(RIGS_ENV, ""))
    paths = rig_locks(device, rigs)
    rig = rigs.get(lock_key(device)) if device else None
    if rig is None:
        paths.update(lock_dir().glob("*.lock"))
    else:
        own = (lock_dir() / f"{key}.lock" for key, owner in rigs.items() if owner == rig)
        paths.update(path for path in own if path.exists())
    return sorted(paths)


def _holder(fd: int) -> str:
    return os.pread(fd, 4096, 0).decode("utf-8", "replace").strip() or "unknown"


def _held_by_ancestors() -> set[Path]:
    """The exported ``$C64_DIAG_LOCK_HELD`` locks whose exporting process still holds them."""
    import fcntl

    held: set[Path] = set()
    for entry in os.environ.get(HELD_ENV, "").splitlines():
        pid, sep, path = entry.partition("@")
        if not sep:
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            if _holder(fd).split(":", 1)[0] == pid:
                held.add(Path(path))
        finally:
            os.close(fd)
    return held


def _acquire(path: Path) -> int | None:
    """Hold ``path`` exclusively, waiting for it if need be; None if interrupted."""
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        pass
    started = time.monotonic()
    print(f"hw_lock: waiting for {path} (held by pid {_holder(fd)})", file=sys.stderr, flush=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except KeyboardInterrupt:
        os.close(fd)
        return None
    print(
        f"hw_lock: acquired {path} after {time.monotonic() - started:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    return fd


def _close_all(held: list[tuple[int, Path]]) -> None:
    for fd, _ in held:
        os.close(fd)


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
        default=None,
        help=f"what the command touches: a URL or a name; picks a lock only via ${RIGS_ENV}",
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

    try:
        rigs = parse_rigs(os.environ.get(RIGS_ENV, ""))
        paths = lock_paths(args.device, rigs)
    except RigMapError as exc:
        print(f"hw_lock: {exc}", file=sys.stderr)
        return 2
    ancestors = _held_by_ancestors()
    if ancestors:
        missing = sorted(rig_locks(args.device, rigs) - ancestors)
        if not missing:
            return _exec(command)
        print(
            f"hw_lock: nested call needs {', '.join(map(str, missing))}, which the enclosing"
            " call does not hold; command not run",
            file=sys.stderr,
        )
        return 2
    lock_dir().mkdir(parents=True, exist_ok=True)
    held: list[tuple[int, Path]] = []
    for path in paths:
        try:
            fd = _acquire(path)
        except OSError as exc:
            _close_all(held)
            print(f"hw_lock: cannot open {path}: {exc}; command not run", file=sys.stderr)
            return 2
        if fd is None:
            _close_all(held)
            print("hw_lock: interrupted while waiting; command not run", file=sys.stderr)
            return 130
        held.append((fd, path))

    stamp = f"{os.getpid()}: {shlex.join(command)}\n".encode()
    for fd, _ in held:
        os.ftruncate(fd, 0)
        os.pwrite(fd, stamp, 0)
        os.set_inheritable(fd, True)
    os.environ[HELD_ENV] = "\n".join(f"{os.getpid()}@{path}" for _, path in held)
    return _exec(command)


if __name__ == "__main__":
    sys.exit(main())
