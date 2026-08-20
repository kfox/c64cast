"""Browse the media a `file =` field could name, inside a root jail.

The read-only sibling of :mod:`config_store`: same jail discipline (roots
resolved once, the depth-capped, skip-dirs-pruned walk shared via
:mod:`fs_walk`, plus the per-entry ``resolve().is_relative_to(root)`` re-check
for a symlinked file) — a second copy of that check would be a second thing to
get wrong, so the walk itself is one function both modules call rather than
two hand-kept-in-sync copies. It stops there — no ``create``, no ``write``, no
``delete`` — so the one new *write*-to-disk surface (uploading a file) can
land as its own change and get reviewed on its own.

**Specs, not refs.** ``ConfigStore`` addresses a file by ``<root-label>/<rel>``
because it *writes* to a named file and ambiguity there would pick the wrong
one. A media entry is a value that goes straight into a scene's ``file =``
field, so it has to be a string :func:`scene_factory.resolve_file_spec` will
resolve — which means the root's *configured* spelling is what a listed entry
is built from, not a synthetic label: a root written ``~/Movies`` lists
``~/Movies/clip.mp4`` (portable across machines, and ``paths.expand_user``
handles the ``~`` at resolve time same as it does for a hand-typed spec), and
a root written ``assets/videos`` lists ``assets/videos/clip.mp4``. The jail
check itself still runs on the *resolved* path, same as `ConfigStore`.

**Directories are entries.** ``resolve_file_spec`` treats a directory as a
randomizer — one file picked at each scene ``setup()`` — which is exactly what
an unset ``file =`` already does against the default asset dir. A picker that
only offered files would hide that. A directory is listed once, for the kind
being browsed, exactly when it directly contains a file of that kind; nothing
is inferred about directories the walk cannot see into (a symlinked directory
is never descended into, matching ``ConfigStore._walk``'s own choice).

**One flat root list, several kinds.** Unlike ``[web].config_roots``, which
names one kind of thing, ``[web].media_roots`` is browsed by every kind at
once — the kind selects which extensions count as a hit during the walk, not
which directories are walked. Empty means the four directories the loader
itself already defaults to (:data:`c64cast.app.scene_factory.DEFAULT_VIDEO_DIR`
and its siblings), which is exactly what an unset ``file =`` resolves to, so
the picker offers what the config would have picked anyway.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .fs_walk import MAX_FILES, walk_dirs
from .scene_factory import (
    AUDIO_EXTS,
    DEFAULT_PROGRAM_DIR,
    DEFAULT_SLIDESHOW_DIR,
    DEFAULT_VIDEO_DIR,
    DEFAULT_WAVEFORM_DIR,
    PICTURE_EXTS,
    PROGRAM_EXTS,
    SID_EXTS,
    VIDEO_EXTS,
)

log = logging.getLogger(__name__)

#: Kind -> the extensions a `file =` entry of that kind ends in. "audio" has no
#: default directory of its own (generative's `audio_source = "file"` requires
#: an explicit `file =`, unlike every other media-bearing scene type — see
#: scene_factory.py's `audio_source == "file"` branch) so it isn't in
#: _DEFAULT_ROOTS, but it is still a browsable kind across whatever roots are
#: configured.
_KIND_EXTS: dict[str, tuple[str, ...]] = {
    "video": VIDEO_EXTS,
    "sid": SID_EXTS,
    "picture": PICTURE_EXTS,
    "program": PROGRAM_EXTS,
    "audio": AUDIO_EXTS,
}

_DEFAULT_ROOTS = (
    DEFAULT_VIDEO_DIR,
    DEFAULT_WAVEFORM_DIR,
    DEFAULT_SLIDESHOW_DIR,
    DEFAULT_PROGRAM_DIR,
)


class MediaStoreError(Exception):
    """Base for every refusal from this module."""


class MediaKindUnknown(MediaStoreError):
    """`kind` isn't one this store knows how to filter for."""


@dataclass(frozen=True)
class MediaRoot:
    """One directory the browser may list media under.

    `spelling` is the root exactly as configured (or one of the packaged
    defaults) — what a listed entry's `spec` is built from. `path` is where
    that spelling actually resolves to, for the jail check only."""

    spelling: str
    path: Path


def _walk(root: MediaRoot) -> Iterator[tuple[Path, list[str]]]:
    """Yield `(directory, filenames)` under `root`, hidden files dropped —
    depth and skip-dirs limiting is `fs_walk.walk_dirs`'s, shared with
    `config_store.ConfigStore._walk`."""
    for here, filenames in walk_dirs(root.path):
        yield here, sorted(f for f in filenames if not f.startswith("."))


def _spec(root: MediaRoot, rel_parts: Sequence[str]) -> str:
    # `wanted` in `MediaStore.__init__` already drops empty/blank spellings,
    # so `rstrip("/")` only empties out a spelling that was itself all
    # slashes (e.g. "/") — the filesystem root, not "no path at all". That
    # root is the one spelling `"/".join` can't just prepend to, or a rel
    # part joins in as "//etc" instead of "/etc".
    spelling = root.spelling.rstrip("/") or "/"
    if not rel_parts:
        return spelling
    if spelling == "/":
        return "/" + "/".join(rel_parts)
    return "/".join((spelling, *rel_parts))


def _candidates(root: MediaRoot, exts: tuple[str, ...]) -> Iterator[tuple[str, bool, Path]]:
    """Yield `(spec, is_dir, path)` for every directory under `root` that
    directly holds a file ending in `exts`, and for every such file itself."""
    for here, filenames in _walk(root):
        hits = [f for f in filenames if f.lower().endswith(exts)]
        if hits:
            yield _spec(root, here.relative_to(root.path).parts), True, here
        for name in hits:
            path = here / name
            # A symlinked file pointing out of the root is an ordinary walk
            # entry (followlinks=False only keeps the walk out of symlinked
            # *directories*) — same escape config_store's own `_walk` guards
            # against.
            if not path.resolve().is_relative_to(root.path):
                continue
            yield _spec(root, path.relative_to(root.path).parts), False, path


def _stat_entry(spec: str, *, is_dir: bool, path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "spec": spec,
        "name": path.name,
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "mtime": stat.st_mtime,
    }


class MediaStore:
    """The browser's view of the host's media directories.

    `roots` are the configured directories (`[web].media_roots`); empty means
    the four directories the loader itself defaults to. A root that doesn't
    exist is dropped with a warning rather than failing the host, matching
    `ConfigStore`'s own choice."""

    def __init__(self, roots: Sequence[str] = (), *, cwd: Path | None = None) -> None:
        wanted = [str(r) for r in roots if str(r).strip()]
        if not wanted:
            wanted = list(_DEFAULT_ROOTS)
        base = cwd if cwd is not None else Path(os.getcwd())
        resolved: list[MediaRoot] = []
        seen: set[Path] = set()
        for spelling in wanted:
            # `spelling` is what a listed entry's spec is built from — the
            # `~` stays a `~` in the spec. It is expanded only to find out
            # where the root actually is.
            candidate = Path(paths.expand_user(spelling))
            location = candidate if candidate.is_absolute() else base / candidate
            real = location.resolve()
            if not real.is_dir():
                log.warning("web console: media root %s is not a directory — ignored", real)
                continue
            if real in seen:
                continue
            seen.add(real)
            resolved.append(MediaRoot(spelling=spelling, path=real))
        self._roots = tuple(resolved)

    @property
    def roots(self) -> tuple[MediaRoot, ...]:
        return self._roots

    @staticmethod
    def kinds() -> tuple[str, ...]:
        return tuple(_KIND_EXTS)

    def index(self, kind: str, q: str = "") -> dict[str, Any]:
        """Every entry of `kind` across every root, plus whichever of their
        containing directories directly hold one.

        `q` is a case-insensitive substring match on the entry's spec, applied
        during the walk — so a search reaches past `MAX_FILES` instead of
        being limited to whatever the cap let through first."""
        exts = _KIND_EXTS.get(kind)
        if exts is None:
            raise MediaKindUnknown(
                f"{kind!r} is not a media kind (know: {', '.join(self.kinds())})"
            )
        needle = q.strip().lower()
        entries: list[dict[str, Any]] = []
        truncated = False

        for root in self._roots:
            for spec, is_dir, path in _candidates(root, exts):
                if needle and needle not in spec.lower():
                    continue
                if len(entries) >= MAX_FILES:
                    truncated = True
                    break
                entry = _stat_entry(spec, is_dir=is_dir, path=path)
                if entry is not None:
                    entries.append(entry)
            if truncated:
                break

        return {
            "kind": kind,
            "roots": [r.spelling for r in self._roots],
            "entries": entries,
            "truncated": truncated,
        }
