"""Browse — and upload into — the media a `file =` field could name, inside a
root jail.

`config_store`'s sibling: same jail discipline (roots resolved once, the
depth-capped, skip-dirs-pruned walk shared via :mod:`fs_walk`, plus the
per-entry ``resolve().is_relative_to(root)`` re-check for a symlinked file) —
a second copy of that check would be a second thing to get wrong, so the walk
itself is one function both modules call rather than two hand-kept-in-sync
copies.

**Two lists, not one.** ``[web].media_read_write`` is a *kind → directory*
table (``video`` → ``assets/videos``, and so on) that is both browsable and
uploadable; ``media_read_only`` is a plain list, browsable only. Which
directory an upload of a given kind lands in is *stated*, not inferred — a
directory named ``clips/`` tells a heuristic nothing about what it holds, and
a brand-new empty one has no contents to guess from either. A kind absent
from the write table (or set to ``""``) simply has nowhere to upload to;
`destination` is what enforces that.

**Never an overwrite.** A name already taken in its destination directory is
never replaced — the incoming file is renamed ``clip-2.mp4``, ``clip-3.mp4``,
… (:func:`_unique_name`), the same numbering :func:`config_store._label_for`
already uses to disambiguate a root label, so there is one way in this
repository to say "that name was taken", not two. The final name is chosen at
*commit* time (right before the rename that lands the file), narrowing the
window between "is this name free" to as little as it can be made;
:func:`os.replace` being atomic means the loser of a genuine race replaces
rather than corrupts, the same trade `ConfigStore.create` already accepts.

**Streamed, not buffered.** :meth:`MediaStore.receive` hands back a context
manager whose `write` takes one chunk at a time, because the body can be a
multi-hundred-megabyte video arriving while this same host is simultaneously
encoding video for a running show — buffering it whole in memory first is not
acceptable. It cannot reuse :func:`c64cast.control.transport.atomic_write_bytes`
for that reason (that one takes its payload as a single `bytes`), so it
reuses its *shape* instead: a temp file in the destination directory
(``.part`` suffix, invisible to :meth:`index` since no kind's extensions end
in it), `flush` + `fsync`, then `os.replace` onto the final name. Any
exception unlinks the ``.part`` and leaves nothing behind.

**Specs, not refs.** ``ConfigStore`` addresses a file by ``<root-label>/<rel>``
because it *writes* to a named file and ambiguity there would pick the wrong
one. A media entry is a value that goes straight into a scene's ``file =``
field, so it has to be a string :func:`scene_factory.resolve_file_spec` will
resolve — which means the root's *configured* spelling is what a listed entry
(and an uploaded one) is built from, not a synthetic label: a root written
``~/Movies`` lists ``~/Movies/clip.mp4`` (portable across machines, and
``paths.expand_user`` handles the ``~`` at resolve time same as it does for a
hand-typed spec), and a root written ``assets/videos`` lists
``assets/videos/clip.mp4``. The jail check itself still runs on the *resolved*
path, same as `ConfigStore`.

**Directories are entries.** ``resolve_file_spec`` treats a directory as a
randomizer — one file picked at each scene ``setup()`` — which is exactly what
an unset ``file =`` already does against the default asset dir. A picker that
only offered files would hide that. A directory is listed once, for the kind
being browsed, exactly when it directly contains a file of that kind; nothing
is inferred about directories the walk cannot see into (a symlinked directory
is never descended into, matching ``ConfigStore._walk``'s own choice).

**The kind comes off the extension, not a parameter.** The five kinds'
extension tuples never overlap (:data:`MEDIA_EXTS` inverts `_KIND_EXTS` on
that assumption), so extension → kind is a function — which is also what
settles what a two-kind `generative` scene (``sid`` and ``audio``) uploads,
without a client ever having to choose.
"""

from __future__ import annotations

import contextlib
import logging
import ntpath
import os
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .fs_walk import MAX_FILES, disambiguate, walk_dirs
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
#: _DEFAULT_WRITE, but it is still a browsable kind across whatever roots are
#: configured, and an upload destination once one is named.
_KIND_EXTS: dict[str, tuple[str, ...]] = {
    "video": VIDEO_EXTS,
    "sid": SID_EXTS,
    "picture": PICTURE_EXTS,
    "program": PROGRAM_EXTS,
    "audio": AUDIO_EXTS,
}

#: Extension -> kind, the inverse of `_KIND_EXTS`. Well-defined because the
#: five tuples above never share an extension; this is what lets an upload's
#: kind come off its name alone.
MEDIA_EXTS: dict[str, str] = {ext: kind for kind, exts in _KIND_EXTS.items() for ext in exts}

#: The four kinds the loader itself already defaults to when a scene's own
#: `file =` is unset (`scene_factory.DEFAULT_VIDEO_DIR` and siblings) — the
#: baseline `[web].media_read_write` merges onto, so naming one kind there
#: (or turning it off with `""`) leaves the rest exactly where they were.
_DEFAULT_WRITE: dict[str, str] = {
    "video": DEFAULT_VIDEO_DIR,
    "sid": DEFAULT_WAVEFORM_DIR,
    "picture": DEFAULT_SLIDESHOW_DIR,
    "program": DEFAULT_PROGRAM_DIR,
}

#: I/O bounding, not a security boundary — same spirit as `fs_walk.MAX_FILES`.
MAX_UPLOAD_BYTES = 512 << 20

#: Independent of `MAX_FILES` (which caps what `index` *returns*) — bounds
#: how many candidates a `q`-filtered scan may examine, so a query matching
#: nothing can't turn one request into a walk of the whole root. An order of
#: magnitude above `MAX_FILES` so an ordinary search past that cap still
#: completes.
_MAX_SCAN = MAX_FILES * 10

#: A name is rejected past this many collisions rather than renamed forever;
#: hitting it means something else is wrong (a script re-uploading in a loop).
_MAX_RENAME_ATTEMPTS = 100

#: The suffix a filesystem itself already caps most names to; enforced here so
#: a name that would fail for an obscure reason fails with a readable one.
_MAX_NAME_BYTES = 255


class MediaStoreError(Exception):
    """Base for every refusal from this module."""


class MediaKindUnknown(MediaStoreError):
    """`kind` isn't one this store knows how to filter for."""


class MediaNameRejected(MediaStoreError):
    """Not a bare, sane file name, or its extension matches no known kind."""


class MediaNotUploadable(MediaStoreError):
    """The kind this name resolves to has nowhere configured to write to."""


class MediaTooLarge(MediaStoreError):
    """Past :data:`MAX_UPLOAD_BYTES`."""


@dataclass(frozen=True)
class MediaRoot:
    """One directory the browser may list media under.

    `spelling` is the root exactly as configured (or one of the packaged
    defaults) — what a listed entry's `spec` is built from. `path` is where
    that spelling actually resolves to, for the jail check only. `writable`
    is true for a root that came from `media_read_write` — the browsable-only
    `media_read_only` entries are never a destination `destination` returns."""

    spelling: str
    path: Path
    writable: bool = False


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
    directly holds a file ending in `exts` *and* passing the jail check, and
    for every such file itself."""
    for here, filenames in _walk(root):
        # Filtered before the directory is offered as an entry, not after —
        # `resolve_file_spec` treats a listed directory as a randomizer that
        # picks a member at each scene `setup()`, so a directory entry built
        # from an unfiltered `hits` would hand back exactly the out-of-jail
        # file the per-file check below refuses to list directly. A symlinked
        # file pointing out of the root is an ordinary walk entry
        # (followlinks=False only keeps the walk out of symlinked
        # *directories*) — same escape config_store's own `_walk` guards
        # against.
        kept = [
            f
            for f in filenames
            if f.lower().endswith(exts) and (here / f).resolve().is_relative_to(root.path)
        ]
        if kept:
            yield _spec(root, here.relative_to(root.path).parts), True, here
        for name in kept:
            path = here / name
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


def _resolve_write_table(read_write: Mapping[str, str]) -> dict[str, str]:
    """`[web].media_read_write` merged onto `_DEFAULT_WRITE`: an empty table
    means the four defaults untouched; naming a kind (including setting it to
    `""`, the off switch) only ever changes that one kind, because a config
    author turning `video` off has no way in TOML to say "and leave the rest
    alone" other than not mentioning them.

    Raises `MediaKindUnknown` for any key outside the five known kinds — left
    unchecked, a misspelled kind (``vidoe``) would resolve as a writable root
    no upload's extension-derived kind could ever reach, and the typo would
    do nothing without a word said about it."""
    if not read_write:
        return dict(_DEFAULT_WRITE)
    unknown = sorted(set(read_write) - _KIND_EXTS.keys())
    if unknown:
        raise MediaKindUnknown(
            f"media_read_write names {unknown!r}, not a media kind (know: {', '.join(_KIND_EXTS)})"
        )
    merged = dict(_DEFAULT_WRITE)
    merged.update(read_write)
    return merged


def _unique_name(directory: Path, name: str) -> tuple[str, bool]:
    """`name`, or the first of `stem-2.ext`, `stem-3.ext`, … not already in
    `directory` — never an overwrite. Numbered by `fs_walk.disambiguate`, the
    same scheme `config_store._label_for` uses to disambiguate a root label —
    one way in this repository to say "that name was taken".

    An incoming name already at `_MAX_NAME_BYTES` is only lengthened by the
    `-2`/`-3`/… suffix, so `taken` rejects any candidate that would cross the
    cap rather than handing it to `os.replace` for a raw, unmapped
    `ENAMETOOLONG`.

    Checked here and used immediately by the caller's `os.replace` — the
    remaining TOCTOU window is the same one `ConfigStore.create` already
    accepts, and `os.replace`'s atomicity means the loser of a genuine race
    replaces rather than corrupts."""

    def taken(candidate: str) -> bool:
        if len(candidate.encode("utf-8")) > _MAX_NAME_BYTES:
            raise MediaNameRejected(f"{candidate!r} is longer than {_MAX_NAME_BYTES} bytes")
        return (directory / candidate).exists()

    stem, suffix = Path(name).stem, Path(name).suffix
    try:
        return disambiguate(stem, suffix, taken, _MAX_RENAME_ATTEMPTS)
    except LookupError as e:
        raise MediaNameRejected(str(e)) from e


def _reject_unless_bare_filename(name: str) -> None:
    if not name or name in {".", ".."}:
        raise MediaNameRejected("a file name is required")
    if "/" in name or "\\" in name:
        raise MediaNameRejected(f"{name!r} is not a bare file name")
    if ntpath.splitdrive(name)[0]:
        # No separator survives this: `PureWindowsPath('D:/media') /
        # 'C:evil.prg'` is `PureWindowsPath('C:evil.prg')`, discarding the
        # left operand entirely — Windows is a first-class target
        # (paths.py's `os.name == "nt"` branches), so a drive-relative name
        # has to be refused by itself, not caught by the separator check
        # above.
        raise MediaNameRejected(f"{name!r} names a drive, not a bare file name")
    if name.startswith("."):
        raise MediaNameRejected(f"{name!r} may not start with a dot")
    if "\x00" in name:
        raise MediaNameRejected(f"{name!r} contains a NUL byte")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise MediaNameRejected(f"{name!r} is longer than {_MAX_NAME_BYTES} bytes")


class Upload:
    """A streamed upload in progress, yielded by `MediaStore.receive`'s `with`
    block. `write` appends one chunk to the `.part` file, raising
    `MediaTooLarge` before it grows past `MAX_UPLOAD_BYTES`. `result` is only
    populated once the block exits without raising and the file has been
    committed to its final name — see `MediaStore.receive`."""

    def __init__(self, kind: str, file: Any) -> None:
        self.kind = kind
        self._file = file
        self.bytes_written = 0
        self.result: dict[str, Any] = {}

    def write(self, chunk: bytes) -> None:
        self.bytes_written += len(chunk)
        if self.bytes_written > MAX_UPLOAD_BYTES:
            raise MediaTooLarge(f"upload is larger than the {MAX_UPLOAD_BYTES}-byte limit")
        self._file.write(chunk)


class MediaStore:
    """The browser's view of the host's media directories, and its one write
    surface: uploading a new file into whichever of them `media_read_write`
    names.

    `read_write` is `[web].media_read_write` (kind -> directory), merged onto
    the loader's own defaults by `_resolve_write_table`; `read_only` is
    `[web].media_read_only`, browsable but never a `destination`. A root that
    doesn't exist is dropped with a warning rather than failing the host,
    matching `ConfigStore`'s own choice. The write directories are resolved
    first and read-only ones second, so a path configured both ways keeps its
    `writable` root — "write paths first" in the docstring is why upload
    directories sort to the front of a listing, too."""

    def __init__(
        self,
        read_write: Mapping[str, str] = {},
        read_only: Sequence[str] = (),
        *,
        cwd: Path | None = None,
    ) -> None:
        base = cwd if cwd is not None else Path(os.getcwd())
        resolved: list[MediaRoot] = []
        seen: dict[Path, MediaRoot] = {}
        write_roots: dict[str, MediaRoot] = {}
        write_missing: dict[str, str] = {}

        def resolve_root(spelling: str, *, writable: bool) -> MediaRoot | None:
            # `spelling` is what a listed entry's spec is built from — the
            # `~` stays a `~` in the spec. It is expanded only to find out
            # where the root actually is.
            candidate = Path(paths.expand_user(spelling))
            location = candidate if candidate.is_absolute() else base / candidate
            real = location.resolve()
            existing = seen.get(real)
            if existing is not None:
                # Write roots resolve first (below), so a path reached a
                # second time here can only be read-only re-naming an
                # already-writable root — never the other way around. If a
                # future refactor reorders the two loops, this catches the
                # drift immediately rather than letting `writable` silently
                # disagree with `_write_roots`.
                assert not (writable and not existing.writable), (
                    f"{spelling!r} resolved to an existing non-writable root "
                    "— write roots must be resolved before read-only ones"
                )
                return existing
            if not real.is_dir():
                log.warning("web console: media root %s is not a directory — ignored", real)
                return None
            root = MediaRoot(spelling=spelling, path=real, writable=writable)
            seen[real] = root
            resolved.append(root)
            return root

        for kind, spelling in _resolve_write_table(read_write).items():
            if not spelling.strip():
                continue
            root = resolve_root(spelling, writable=True)
            if root is not None:
                write_roots[kind] = root
            else:
                write_missing[kind] = spelling

        for spelling in read_only:
            spelling = str(spelling)
            if spelling.strip():
                resolve_root(spelling, writable=False)

        self._roots = tuple(resolved)
        self._write_roots = write_roots
        self._write_missing = write_missing

    @property
    def roots(self) -> tuple[MediaRoot, ...]:
        return self._roots

    @staticmethod
    def kinds() -> tuple[str, ...]:
        return tuple(_KIND_EXTS)

    def destination(self, name: str) -> tuple[str, Path]:
        """The `(kind, directory)` an upload named `name` would land in.

        Structural checks first — a bare file name, no `..`, no drive
        prefix, no NUL, no leading dot, not empty, not absurdly long — then
        the kind read off the extension (unambiguous; see `MEDIA_EXTS`),
        then whether that kind has anywhere configured to write to at all.
        This only says which root the name belongs to; `receive` — its only
        caller — is what re-checks the joined path against that root before
        the rename that actually lands the file, since `_unique_name` runs
        after this and could in principle lengthen the name back past the
        jail."""
        _reject_unless_bare_filename(name)
        ext = Path(name).suffix.lower()
        kind = MEDIA_EXTS.get(ext)
        if kind is None:
            raise MediaNameRejected(f"{name!r} has no extension a known media kind ends in")
        root = self._write_roots.get(kind)
        if root is not None:
            return kind, root.path
        missing = self._write_missing.get(kind)
        if missing is not None:
            raise MediaNotUploadable(
                f"the {kind} upload directory {missing!r} is configured but does not exist"
            )
        raise MediaNotUploadable(f"uploading a {kind} file is not configured on this host")

    @contextlib.contextmanager
    def receive(self, name: str) -> Iterator[Upload]:
        """Stream an upload named `name` into its destination directory.

        Writes to a `.part` temp file in that directory (never visible to
        `index`, since `.part` ends no kind's extension tuple) so a crash or a
        refused chunk mid-transfer leaves nothing behind; on a clean exit the
        file is `fsync`'d, `_unique_name` picks the final name, and the
        joined `directory / final_name` is re-checked against the root
        before `os.replace` lands it — the check `destination`'s docstring
        promises a caller runs against the *result*, run here since `receive`
        is that only caller. A commit-time `OSError`/`ValueError` (a full
        disk, a name `os.replace` itself refuses) surfaces as
        `MediaStoreError`, same as every other refusal from this module,
        rather than an untyped exception a caller's error mapping can't
        classify. The `.part` is unlinked on any failure — streaming or
        commit — and cleanup itself never replaces the failure that
        triggered it. `upload.result` is set, and the commit logged, only
        once that succeeds."""
        kind, directory = self.destination(name)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".part")
        file = os.fdopen(fd, "wb")
        upload = Upload(kind, file)

        def abort(exc: BaseException) -> None:
            with contextlib.suppress(OSError):
                file.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            log.warning(
                "web console: upload of %r aborted after %d bytes (%s)",
                # `%r` rather than `%s` because `name` is an untrusted upload
                # filename that `_reject_unless_bare_filename` never rejects
                # for an embedded newline — `repr` cannot emit one, so the
                # waiver is for CodeQL modeling neither as a sanitizer.
                # codeql[py/log-injection]
                name,
                upload.bytes_written,
                type(exc).__name__,
            )

        try:
            yield upload
        except BaseException as e:
            abort(e)
            raise

        try:
            file.flush()
            os.fsync(file.fileno())
            file.close()
            final_name, renamed = _unique_name(directory, name)
            _reject_unless_bare_filename(final_name)
            target = directory / final_name
            if not target.resolve().is_relative_to(directory):
                raise MediaNameRejected(f"{final_name!r} would land outside its media root")
            os.replace(tmp_path, target)
        except BaseException as e:
            abort(e)
            if isinstance(e, MediaStoreError) or not isinstance(e, (OSError, ValueError)):
                raise
            raise MediaStoreError(f"could not commit upload {name!r} to {directory}: {e}") from e

        root = self._write_roots[kind]
        upload.result = {
            "spec": _spec(root, (final_name,)),
            "name": final_name,
            "kind": kind,
            "bytes": upload.bytes_written,
            "renamed": renamed,
        }
        log.info(
            "web console: received %r as %r (kind=%s, %d bytes%s)",
            # Both `%r`, and both waived for the same reason as `abort`'s log
            # line above: `name` is the untrusted upload filename itself, and
            # `final_name` inherits whatever `name` put in its stem —
            # `disambiguate` only ever appends a numeric `-N` suffix to it.
            # codeql[py/log-injection]
            name,
            # codeql[py/log-injection]
            final_name,
            kind,
            upload.bytes_written,
            ", renamed" if renamed else "",
        )

    def index(self, kind: str, q: str = "") -> dict[str, Any]:
        """Every entry of `kind` across every root, plus whichever of their
        containing directories directly hold one.

        `q` is a case-insensitive substring match on the entry's spec, applied
        during the walk — so a search reaches past `MAX_FILES` instead of
        being limited to whatever the cap let through first. That means a
        query matching nothing would otherwise walk every root in full, so a
        scan past `_MAX_SCAN` candidates also sets `truncated`, independent
        of how many (if any) entries matched."""
        exts = _KIND_EXTS.get(kind)
        if exts is None:
            raise MediaKindUnknown(
                f"{kind!r} is not a media kind (know: {', '.join(self.kinds())})"
            )
        needle = q.strip().lower()
        entries: list[dict[str, Any]] = []
        truncated = False
        visited = 0

        for root in self._roots:
            for spec, is_dir, path in _candidates(root, exts):
                visited += 1
                if visited > _MAX_SCAN:
                    truncated = True
                    break
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
