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
"""

from __future__ import annotations

import os
from collections.abc import Iterator
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
