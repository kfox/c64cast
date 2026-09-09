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
the exact silent green `mutation-ready` exists to remove.

PEP 552 puts the invalidation mode in bit 0 of the 32-bit little-endian flags
word at offset 4 of the header; bit 1 is `check_source`. `checked-hash` sets
both.

Only the files an import can actually reach are checked: a `.pyc` whose cache
tag is not this interpreter's is never loaded here, and neither is one whose
source is gone. A checkout that has been run under more than one Python minor,
or across a module rename, carries plenty of both — this tree carried 111 when
the check was written, all orphans — and failing on those would make the check
noise, which is how a check gets deleted.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_FLAGS_OFFSET = 4
_HEADER_PREFIX = 8
_ARMED = 0b11  # bit 0 hash-based, bit 1 check_source


def _flags(pyc: Path) -> int:
    with pyc.open("rb") as f:
        header = f.read(_HEADER_PREFIX)
    if len(header) < _HEADER_PREFIX:
        raise ValueError(f"{pyc}: truncated header ({len(header)} bytes)")
    return struct.unpack_from("<I", header, _FLAGS_OFFSET)[0]


def _loadable_here(pyc: Path) -> bool:
    """True when importing this module in this interpreter would read `pyc`."""
    stem, _, tag = pyc.stem.partition(".")
    return tag == sys.implementation.cache_tag and (pyc.parent.parent / f"{stem}.py").exists()


def unarmed(roots: list[str]) -> list[Path]:
    """Every loadable `.pyc` under `roots` that is not checked-hash, sorted."""
    found = []
    for root in roots:
        for pyc in Path(root).rglob("__pycache__/*.pyc"):
            if _loadable_here(pyc) and _flags(pyc) & _ARMED != _ARMED:
                found.append(pyc)
    return sorted(found)


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["c64cast", "tests", "scripts"]
    stale = unarmed(roots)
    if not stale:
        return 0
    print(
        f"{len(stale)} compiled module(s) are still timestamp-based, so a "
        "same-second mutation would run stale bytecode and report a false "
        "green. Run `make mutation-ready`.",
        file=sys.stderr,
    )
    for pyc in stale[:10]:
        print(f"  {pyc}", file=sys.stderr)
    if len(stale) > 10:
        print(f"  ... and {len(stale) - 10} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
