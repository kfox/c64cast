#!/usr/bin/env python3
"""Fail unless every compiled module under the given roots is hash-based.

`make mutation-ready` runs `compileall --invalidation-mode checked-hash` so a
mutation applied and reverted inside one second is not silently run off stale
bytecode: CPython validates a timestamp-mode `.pyc` against the source mtime in
whole seconds, and a same-length edit inside that second leaves both unchanged.
The conversion is real but it is not durable, and nothing used to say when it
had lapsed. Four routine things un-arm the tree with no output at all:

* `make clean` removes every `__pycache__`, and the next import writes a fresh
  timestamp-mode `.pyc`;
* a new worktree — the adverse-review campaign's normal working shape — starts
  with no `__pycache__` at all;
* a `uv sync` that moves the Python minor changes the magic tag, so the armed
  files are ignored and rewritten;
* `make test PY=python` does the same.

So the state is checked rather than assumed. A mutation proof run against
timestamp-mode bytecode reports green whatever the mutation says, which reads as
"the test does not pin this line" and costs a rewrite of a test that was fine —
the exact silent green `mutation-ready` exists to remove. `make mutation-check`
is this check alone, which is the one to run when the arming happened at some
earlier point and a proof is about to be believed.

PEP 552 puts the invalidation mode in bit 0 of the 32-bit little-endian flags
word at offset 4 of the header; bit 1 is `check_source`. `checked-hash` sets
both, `unchecked-hash` sets only bit 0 — and that one is hash-based yet skips
validation entirely, so it misses a mutation the same way with a different
cause. Both bits are required.

**The scan walks sources, not `__pycache__`.** `importlib.util.cache_from_source`
is the same function an import uses to pick a file, so asking it settles the
cache tag, the optimization level and a dotted module name together, and the
answer is the file this interpreter would actually read. Walking the cache
directory instead meant reimplementing that name, which got both the tag and the
`opt-N` suffix wrong in turn — and the second way round was worse than the
first: it reported optimized bytecode that a non-`-O` `compileall` will never
rewrite, so the check failed permanently while printing a remedy that does
nothing.

**Absence of bytecode is itself un-armed, and it is the commonest shape.** An
earlier version of this file said absence could not be told from "the module was
never imported" and so declined to report it — which made three of the four
lapses above silent passes, including the fresh worktree. The claim was simply
wrong: `compileall` compiles every source under a root whether or not anything
imports it, so after `make mutation-ready` there is no such thing as a source
without bytecode (403 of 403 here, measured). A missing `.pyc` therefore means
the arming has not happened or has lapsed since — and it is the dangerous
shape, because the first import then writes a *timestamp-mode* file, which is
exactly what makes a mutate/revert inside one second run stale bytecode.

Two floors, because they answer different questions and each one hid the other
when it stood alone. Per root, at least one `*.py`: a mistyped or renamed root
has no sources at all, and a global count hid that behind whichever root did
have some. Per source, a `.pyc` that is armed: that is the state a proof
actually rests on.
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
    """The invalidation mode, for the failure line. Not masked with `_ARMED`
    first: both sentinels are negative, and masking maps them onto real modes —
    `-1 & 0b11` is 3, which would look up "armed" and print a raw flags word,
    and `-2 & 0b11` is 2, which would report a missing file as
    "check_source without hash-based"."""
    if flags in _MODE_NAMES:
        return _MODE_NAMES[flags]
    return _MODE_NAMES.get(flags & _ARMED, f"flags {flags:#04x}")


def _sources(root: str) -> list[Path]:
    """The Python sources under `root`, or `root` itself when it names one.

    A root that is a single file has no `rglob("*.py")` results, which the
    per-root floor would report as "no Python sources under
    scripts/check_hash_based_pycs.py" — wrong about a file that is Python."""
    path = Path(root)
    if path.is_file():
        return [path] if path.suffix == ".py" else []
    return sorted(path.rglob("*.py"))


def scan(roots: list[str]) -> tuple[list[tuple[Path, int]], dict[str, int]]:
    """`(unarmed, sources_per_root)` over the bytecode an import here would read.

    `sources_per_root` is returned rather than a total, because "this root has
    no Python in it" is a mistyped or renamed root and a global count hides it
    behind whichever root does have some.
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
                # Fail closed and keep scanning: a file we cannot read is a
                # file we cannot vouch for, and aborting here would skip the
                # remaining roots and report on a partial tree. A truncated
                # header is deliberately NOT caught — that is a corrupt `.pyc`
                # rather than an arming question, and it stops with the path in
                # the message.
                unarmed.append((pyc, _UNREADABLE))
                continue
            if flags & _ARMED != _ARMED:
                unarmed.append((pyc, flags))
        sources[root] = found
    return unarmed, sources


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["c64cast", "tests", "scripts"]
    stale, sources = scan(roots)
    empty = [root for root, n in sources.items() if not n]
    if empty:
        print(
            f"no Python sources under {', '.join(empty)} — nothing was checked there, "
            "so this is not a pass. Run `make mutation-check` from the repository root.",
            file=sys.stderr,
        )
        return 1
    if not stale:
        return 0
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
