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

Only the files an import can actually reach are checked: a `.pyc` whose cache
tag is not this interpreter's is never loaded here, and neither is one whose
source is gone. A checkout that has been run under more than one Python minor,
or across a module rename, carries plenty of both — this tree carried 111 when
the check was written, all orphans — and failing on those would make the check
noise, which is how a check gets deleted.

What this cannot tell you is that a module *has* compiled bytecode. Absence is
the un-armed state's other shape (a wiped `__pycache__`, a fresh worktree), and
it is indistinguishable from "the module was never imported". So the scan is
required to have inspected something: a wrong working directory or a renamed
root would otherwise be a silent pass, which is the failure mode this file
exists to remove.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_FLAGS_OFFSET = 4
_HEADER_PREFIX = 8
_HASH_BASED = 0b01
_CHECK_SOURCE = 0b10
_ARMED = _HASH_BASED | _CHECK_SOURCE

_MODE_NAMES = {
    -1: "could not be read",
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


def _loadable_here(pyc: Path) -> bool:
    """True when importing this module in this interpreter would read `pyc`.

    The name is `<module>.<cache tag>[.opt-N].pyc`, so the tag is the *second*
    dot-separated part and not everything after the first dot — reading it that
    way skipped every optimized build, which an import under `-O` does read.
    """
    parts = pyc.name.split(".")
    if len(parts) < 3:
        return False
    module, tag = parts[0], parts[1]
    return tag == sys.implementation.cache_tag and (pyc.parent.parent / f"{module}.py").exists()


def scan(roots: list[str]) -> tuple[list[tuple[Path, int]], int]:
    """`(unarmed, inspected)` over the loadable `.pyc` files under `roots`.

    `inspected` is returned rather than inferred from the result, because an
    empty result and an empty scan are the same value and very different facts.
    """
    unarmed: list[tuple[Path, int]] = []
    inspected = 0
    for root in roots:
        for pyc in sorted(Path(root).rglob("__pycache__/*.pyc")):
            if not _loadable_here(pyc):
                continue
            inspected += 1
            try:
                flags = _flags(pyc)
            except OSError:
                # Fail closed and keep scanning: a file we cannot read is a
                # file we cannot vouch for, and aborting here would skip the
                # remaining roots and report on a partial tree. A truncated
                # header is deliberately NOT caught — that is a corrupt `.pyc`
                # rather than an arming question, and it stops with the path in
                # the message.
                unarmed.append((pyc, -1))
                continue
            if flags & _ARMED != _ARMED:
                unarmed.append((pyc, flags))
    return unarmed, inspected


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["c64cast", "tests", "scripts"]
    stale, inspected = scan(roots)
    if not inspected:
        print(
            f"no compiled modules found under {', '.join(roots)} for "
            f"{sys.implementation.cache_tag} — nothing was checked, so this is not a "
            "pass. Run `make mutation-ready` from the repository root.",
            file=sys.stderr,
        )
        return 1
    if not stale:
        return 0
    print(
        f"{len(stale)} of {inspected} compiled module(s) are not checked-hash, so a "
        "same-second mutation would run stale bytecode and report a false green. "
        "Run `make mutation-ready`.",
        file=sys.stderr,
    )
    for pyc, flags in stale[:10]:
        print(f"  {pyc}: {_MODE_NAMES.get(flags & _ARMED, f'flags {flags:#04x}')}", file=sys.stderr)
    if len(stale) > 10:
        print(f"  ... and {len(stale) - 10} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
