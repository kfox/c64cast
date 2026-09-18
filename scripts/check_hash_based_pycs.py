#!/usr/bin/env python3
"""Fail unless every compiled module under the given roots is hash-based.

`make mutation-ready` runs `compileall --invalidation-mode checked-hash` so a
mutation applied and reverted inside one second is not silently run off stale
bytecode: CPython validates a timestamp-mode `.pyc` against the source mtime in
whole seconds, and a same-length edit inside that second leaves both unchanged.
The arming is not durable — `make clean`, a fresh worktree, a `uv sync` that
moves the Python minor (a new cache tag), and `make test PY=python` each un-arm
the tree with no output at all — so `make mutation-check` runs this alone when
a proof is about to be believed.

PEP 552 puts the invalidation mode in bit 0 of the 32-bit little-endian flags
word at offset 4 of the header; bit 1 is `check_source`. `checked-hash` sets
both; `unchecked-hash` sets only bit 0 and skips validation entirely, missing a
mutation the same way. Both bits are required.

The scan walks sources, not `__pycache__`: `importlib.util.cache_from_source`
is the function an import uses to pick a file, so it settles the cache tag, the
optimization level and the module name together.

A missing `.pyc` counts as un-armed rather than as "never imported":
`compileall` compiles every source under a root whether or not anything imports
it, so after `make mutation-ready` there is none. It is also the dangerous
shape, because the first import then writes a *timestamp-mode* file.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

_FLAGS_OFFSET = 4
_HEADER_PREFIX = 8
_HASH_BASED = 0b01
_CHECK_SOURCE = 0b10
_ARMED = _HASH_BASED | _CHECK_SOURCE
_UNREADABLE = -1
_ABSENT = -2

_MODE_NAMES = {
    _UNREADABLE: "could not be read",
    _ABSENT: "has no compiled bytecode — nothing here is armed",
    0b00: "timestamp-based",
    0b01: "unchecked-hash (hash-based, but never validated)",
    0b10: "check_source without hash-based",
}


def _flags(pyc: Path) -> int:
    with pyc.open("rb") as f:
        header = f.read(_HEADER_PREFIX)
    if len(header) < _HEADER_PREFIX:
        raise ValueError(f"{pyc}: truncated header ({len(header)} bytes)")
    return struct.unpack_from("<I", header, _FLAGS_OFFSET)[0]


def describe(flags: int) -> str:
    """The invalidation mode, for the failure line.

    Sentinels are matched before masking: both are negative, and `-2 & 0b11` is
    2, which would report a missing file as "check_source without hash-based".
    """
    if flags in _MODE_NAMES:
        return _MODE_NAMES[flags]
    return _MODE_NAMES.get(flags & _ARMED, f"flags {flags:#04x}")


def _sources(root: str) -> list[Path]:
    """The Python sources under `root`, or `root` itself when it names one.

    A single-file root has no `rglob("*.py")` results, which the per-root floor
    would otherwise report as having no Python sources in it."""
    path = Path(root)
    if path.is_file():
        return [path] if path.suffix == ".py" else []
    return sorted(path.rglob("*.py"))


def scan(roots: list[str]) -> tuple[list[tuple[Path, int]], dict[str, int]]:
    """`(unarmed, sources_per_root)` over the bytecode an import here would read.

    Per root rather than a total: "this root has no Python in it" is a mistyped
    or renamed root, and a global count hides it behind the roots that do have
    some.
    """
    unarmed: list[tuple[Path, int]] = []
    sources: dict[str, int] = {}
    for root in roots:
        found = 0
        for src in _sources(root):
            found += 1
            pyc = Path(importlib.util.cache_from_source(str(src)))
            if not pyc.exists():
                unarmed.append((pyc, _ABSENT))
                continue
            try:
                flags = _flags(pyc)
            except OSError:
                unarmed.append((pyc, _UNREADABLE))
                continue
            if flags & _ARMED != _ARMED:
                unarmed.append((pyc, flags))
        sources[root] = found
    return unarmed, sources


def main(argv: list[str]) -> int:
    roots = argv[1:]
    if not roots:
        print(
            "no roots given, so nothing was checked. The roots are the Makefile's "
            "SOURCE_ROOTS — run `make mutation-check` from the repository root.",
            file=sys.stderr,
        )
        return 1
    stale, sources = scan(roots)
    empty = [root for root, n in sources.items() if not n]

    if empty:
        print(
            f"no Python sources under {', '.join(empty)} — nothing was checked there, "
            "so this is not a pass. Run `make mutation-check` from the repository root.",
            file=sys.stderr,
        )
    if stale:
        print(
            f"{len(stale)} module(s) are not armed for a mutation proof, so a same-second "
            "mutation would run stale bytecode and report a false green. "
            "Run `make mutation-ready`.",
            file=sys.stderr,
        )
        for pyc, flags in stale[:10]:
            print(f"  {pyc}: {describe(flags)}", file=sys.stderr)
        if len(stale) > 10:
            print(f"  ... and {len(stale) - 10} more", file=sys.stderr)
    return 1 if empty or stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
