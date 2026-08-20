"""The depth-capped, skip-dirs-pruned directory walk shared by `config_store`
and `media_store`'s root jails.

Both modules walk a resolved root with `os.walk(followlinks=False)`, capping
depth and pruning the same hidden/build directories — a second copy of that
check is a second thing to get wrong, so it lives here once. What each caller
still does on its own: filtering *filenames* (by suffix in `config_store`, by
extension-per-kind in `media_store`) and the per-entry
`resolve().is_relative_to(root)` symlink-escape re-check, since a directory
that passes the walk isn't itself a candidate for that check the same way a
file is.

`disambiguate` lives here for the same reason: both modules number a
collided name `base-2`, `base-3`, … the same way, so it's one function
callers plug their own `taken` check into rather than two hand-kept-in-sync
loops.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

#: Caps on the listing walk. Both are about keeping a hostile or merely
#: enormous directory from turning one request into minutes of I/O; neither
#: is a security boundary.
MAX_FILES = 500
MAX_DEPTH = 8

SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})


def walk_dirs(root_path: Path) -> Iterator[tuple[Path, list[str]]]:
    """Yield `(directory, filenames)` under `root_path`, depth-capped at
    `MAX_DEPTH` and with `SKIP_DIRS` (and other dot-directories) pruned.
    `filenames` is unfiltered — each caller applies its own suffix/extension
    rule to it."""
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        here = Path(dirpath)
        if len(here.relative_to(root_path).parts) >= MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
            )
        yield here, filenames


def disambiguate(
    base: str, suffix: str, taken: Callable[[str], bool], max_attempts: int | None = None
) -> tuple[str, bool]:
    """`f"{base}{suffix}"`, or the first `f"{base}-{n}{suffix}"` for which
    `taken` says `False` — never returns a name `taken` calls collided.
    Returns `(name, renamed)`.

    `max_attempts` bounds the search and raises `LookupError` past it; pass it
    when `taken` costs a filesystem stat per call, so a directory that's
    already full of `base-N` collisions can't turn one request into an
    unbounded scan. Omit it when `taken` checks an already-enumerated,
    in-memory set, which terminates on its own once `n` exceeds the set's
    size."""
    candidate = f"{base}{suffix}"
    n = 2
    while taken(candidate):
        if max_attempts is not None and n > max_attempts:
            raise LookupError(f"too many names already taken like {base!r}")
        candidate = f"{base}-{n}{suffix}"
        n += 1
    return candidate, n > 2
