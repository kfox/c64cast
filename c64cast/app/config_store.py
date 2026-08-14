"""Browse, read and write config files for the web console, inside a root jail.

This is the first thing in c64cast that hands part of the host's filesystem to
the network, so the boundary is a module of its own rather than a few checks
inside a route: the jail is testable without HTTP, and a second caller can't
reach the filesystem by a different path than the first.

**Refs, not paths.** The wire identifier for a file is ``"<root-label>/<rel>"``,
where the label is the root directory's own basename. Bare relative paths were
the obvious alternative and were rejected: with more than one root they are
ambiguous, and the disambiguation rule ("first root that has it") is exactly the
kind of thing that quietly resolves to a different file than the operator
expected. A ref names one file under one named root or it names nothing.

**The check that actually holds is ``resolve()`` + ``is_relative_to``.** Roots
are resolved once at construction, so a symlink *inside* a root that points out
of it fails the check like any other escape — rejecting ``..`` up front only
buys a better error message. Listing walks with ``followlinks=False`` for the
same reason, plus the loop protection that comes free with it.

**A write validates first, and keeps one copy of what it replaced.** Refusing to
save a config that cannot run is the point of having the editor talk to the
loader at all; the ``validate`` route is what lets a UI see the error before it
tries. The replaced text goes to a dotfile sibling — invisible to the listing,
recoverable by hand — because a remote overwrite of a show config otherwise has
no undo at all.

What a ref bounds is *which files are edited*, not what a config can then
reach: a saved TOML names media paths and URLs that a session will open. Remote
config write access is equivalent to local shell-ish reach, which is why the
full token gates it and why the viewer role can't write at all.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c64cast.control.transport import atomic_write_text

from . import config as cfgmod
from . import config_serialize, introspect, paths

log = logging.getLogger(__name__)

#: The only extension the browser will read or write. A config console that can
#: open arbitrary files is a file manager, which is a much larger promise.
SUFFIX = ".toml"

#: Caps on the listing walk and on a single file. All three are about keeping a
#: hostile or merely enormous directory from turning one request into minutes of
#: I/O; none of them is a security boundary.
MAX_FILES = 500
MAX_DEPTH = 8
MAX_BYTES = 1 << 20

_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})


class ConfigStoreError(Exception):
    """Base for every refusal from this module. Messages are end-user readable."""


class PathRejected(ConfigStoreError):
    """The ref names nothing this store is willing to touch."""


class ConfigNotFound(ConfigStoreError):
    """The ref is legal but there is no such file."""


class ConfigTooLarge(ConfigStoreError):
    """Past :data:`MAX_BYTES` — refused rather than read into memory."""


class ConfigInvalid(ConfigStoreError):
    """A write was refused because the text does not load.

    Carries the same report shape :meth:`ConfigStore.validate_text` returns, so
    a caller can render the reason instead of just the failure."""

    def __init__(self, report: dict[str, Any]):
        super().__init__(report.get("error") or "config did not validate")
        self.report = report


@dataclass(frozen=True)
class Root:
    """One directory the browser may see, and the label refs address it by."""

    label: str
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "path": str(self.path)}


def _label_for(path: Path, taken: set[str]) -> str:
    base = path.name or "root"
    label = base
    n = 2
    while label in taken:
        label = f"{base}-{n}"
        n += 1
    return label


@contextmanager
def _capture_errors() -> Iterator[list[str]]:
    """Collect what the validators log while they run.

    ``validate_configs`` writes the diagnostic to the log and raises an exit
    code — fine for a CLI whose user is looking at the terminal, useless for a
    browser. Rather than restructure eight validators to return messages, read
    the messages they already produce."""
    messages: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                messages.append(record.getMessage())

    handler = _Collector()
    logger = logging.getLogger("c64cast")
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


def _unknown_dicts(keys: Sequence[cfgmod.UnknownKey]) -> list[dict[str, Any]]:
    return [{"section": k.section, "key": k.key, "hint": k.hint} for k in keys]


def _value(val: object) -> Any:
    """Coerce a loaded config value to something ``json`` can carry. Everything
    here came from TOML or a dataclass default, so the only surprises are tuples
    and the odd enum-ish string wrapper."""
    if isinstance(val, (tuple, list)):
        return [_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _value(v) for k, v in val.items()}
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    return str(val)


def describe(cfg: cfgmod.Config) -> dict[str, Any]:
    """The loaded config as form data: every field's value plus ``is_default``.

    ``is_default`` is the same comparison ``config_serialize._should_emit``
    makes when deciding whether a field is worth writing, which is what lets a
    UI offer "show only what I've changed" without the server deciding for it.
    The scene field lists come from ``introspect`` already filtered by
    ``applies_to``, so a scene's form can't offer a knob its type ignores."""
    blank = cfgmod.Config()
    sections: list[dict[str, Any]] = []
    for sd in introspect.config_sections():
        section = getattr(cfg, sd.name)
        default_section = getattr(blank, sd.name)
        fields: list[dict[str, Any]] = []
        for fd in sd.fields:
            if (sd.name, fd.name) in config_serialize.SECRET_FIELDS:
                continue
            value = getattr(section, fd.name)
            fields.append(
                {
                    "name": fd.name,
                    "value": _value(value),
                    "is_default": value == getattr(default_section, fd.name),
                }
            )
        sections.append({"name": sd.name, "fields": fields})

    field_docs = {st.name: st.fields for st in introspect.scene_types()}
    blank_scene = cfgmod.SceneCfg()
    scenes: list[dict[str, Any]] = []
    for sc in cfg.scenes:
        docs = field_docs.get(sc.type, ())
        fields = []
        for fd in docs:
            if fd.name == "overlays":
                continue
            value = getattr(sc, fd.name)
            fields.append(
                {
                    "name": fd.name,
                    "value": _value(value),
                    "is_default": value == getattr(blank_scene, fd.name),
                }
            )
        scenes.append(
            {
                "type": sc.type,
                "name": sc.name,
                "fields": fields,
                "overlays": [_value(ov) for ov in sc.overlays],
            }
        )
    return {"sections": sections, "scenes": scenes}


class ConfigStore:
    """The browser's view of the filesystem: a few named roots and nothing else.

    ``roots`` are the configured directories (``[web].config_roots``); an empty
    list means the working directory, which is where a ``c64cast --serve`` run
    launched from a show folder already has its configs. A root that doesn't
    exist is dropped with a warning rather than failing the host — the console
    is still useful for starting the config it was launched with."""

    def __init__(self, roots: Sequence[str] = (), *, cwd: Path | None = None) -> None:
        wanted = [paths.expand_user(r) for r in roots if str(r).strip()]
        if not wanted:
            wanted = [str(cwd) if cwd is not None else os.getcwd()]
        resolved: list[Root] = []
        taken: set[str] = set()
        for raw in wanted:
            path = Path(raw).expanduser().resolve()
            if not path.is_dir():
                log.warning("web console: config root %s is not a directory — ignored", path)
                continue
            if any(path == r.path for r in resolved):
                continue
            label = _label_for(path, taken)
            taken.add(label)
            resolved.append(Root(label=label, path=path))
        self._roots = tuple(resolved)
        self._by_label = {r.label: r for r in self._roots}

    @property
    def roots(self) -> tuple[Root, ...]:
        return self._roots

    # -- refs ---------------------------------------------------------------

    def resolve(self, ref: str) -> Path:
        """Turn a wire ref into an absolute path inside a root, or refuse.

        The returned path need not exist — a write to a new file is legal — but
        it is always a real location under a root, symlinks followed."""
        parts = [p for p in str(ref).replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts:
            raise PathRejected("no config path given")
        if ".." in parts:
            raise PathRejected(f"{ref!r} leaves its config root")
        root = self._by_label.get(parts[0])
        if root is None:
            known = ", ".join(r.label for r in self._roots) or "none configured"
            raise PathRejected(f"{parts[0]!r} is not a config root (roots: {known})")
        rest = parts[1:]
        if not rest:
            raise PathRejected(f"{ref!r} names a config root, not a file in it")
        if not rest[-1].lower().endswith(SUFFIX):
            raise PathRejected(f"{ref!r} is not a {SUFFIX} file")
        target = root.path.joinpath(*rest).resolve()
        if not target.is_relative_to(root.path):
            raise PathRejected(f"{ref!r} leaves its config root")
        return target

    def ref_for(self, path: Path) -> str | None:
        """The ref that addresses `path`, or None if no root contains it."""
        resolved = Path(path).resolve()
        for root in self._roots:
            if resolved.is_relative_to(root.path):
                rel = resolved.relative_to(root.path)
                return "/".join((root.label, *rel.parts))
        return None

    # -- listing ------------------------------------------------------------

    def index(self) -> dict[str, Any]:
        """Every config under every root, plus the roots themselves."""
        files: list[dict[str, Any]] = []
        truncated = False
        for root in self._roots:
            for path in self._walk(root):
                if len(files) >= MAX_FILES:
                    truncated = True
                    break
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rel = path.relative_to(root.path)
                files.append(
                    {
                        "path": "/".join((root.label, *rel.parts)),
                        "root": root.label,
                        "name": path.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
            if truncated:
                break
        return {
            "roots": [r.as_dict() for r in self._roots],
            "files": files,
            "truncated": truncated,
        }

    def _walk(self, root: Root) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
            here = Path(dirpath)
            if len(here.relative_to(root.path).parts) >= MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
                )
            for name in sorted(filenames):
                if name.startswith(".") or not name.lower().endswith(SUFFIX):
                    continue
                path = here / name
                # `followlinks=False` keeps the walk out of symlinked
                # *directories*, but a symlinked file is an ordinary entry —
                # and listing one that `read` would then refuse is worse than
                # not listing it.
                if not path.resolve().is_relative_to(root.path):
                    continue
                yield path

    # -- read ---------------------------------------------------------------

    def read(self, ref: str) -> dict[str, Any]:
        """The file's text, plus whatever the loader can say about it.

        A file that doesn't parse still returns its text with an ``error`` — the
        console's first job when a config is broken is to show it to you."""
        path = self.resolve(ref)
        text = self._read_text(path)
        try:
            stat = path.stat()
        except OSError as e:
            raise ConfigNotFound(f"{ref}: {e}") from e
        out: dict[str, Any] = {
            "path": ref,
            "abs_path": str(path),
            "text": text,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "kind": "config",
            "systems": [],
            "unknown_keys": [],
            "error": None,
            "form": None,
        }
        try:
            loaded = cfgmod.load_master(str(path))
        except (cfgmod.ConfigError, ValueError) as e:
            out["error"] = str(e)
            return out
        out["unknown_keys"] = _unknown_dicts(loaded.unknown_keys)
        if loaded.is_ensemble:
            # Masters are authored across several files and `config_serialize`
            # refuses them by design, so there is no form to generate — the raw
            # text editor is the whole story for one.
            out["kind"] = "ensemble"
            out["systems"] = list(loaded.names)
            return out
        out["form"] = describe(loaded.cfgs[0])
        return out

    def _read_text(self, path: Path) -> str:
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ConfigTooLarge(f"{path.name} is larger than {MAX_BYTES} bytes")
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise ConfigNotFound(f"no such config: {path.name}") from e
        except OSError as e:
            raise ConfigNotFound(f"could not read {path.name}: {e}") from e
        except UnicodeDecodeError as e:
            raise PathRejected(f"{path.name} is not UTF-8 text") from e

    # -- validate + write ---------------------------------------------------

    def validate_text(self, text: str, ref: str | None = None) -> dict[str, Any]:
        """Load `text` as if it were saved, without saving it.

        The scratch file goes in the *target's own directory* rather than a temp
        dir: an ensemble master resolves its per-system paths relative to
        itself, so validating one anywhere else would report missing files that
        are not missing."""
        directory = self._scratch_dir(ref)
        report: dict[str, Any] = {
            "ok": False,
            "error": None,
            "messages": [],
            "unknown_keys": [],
            "systems": [],
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".c64cast-check-", suffix=SUFFIX, dir=directory)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            with _capture_errors() as messages:
                try:
                    loaded = cfgmod.load_master(str(tmp))
                    from .session import SessionConfigError, validate_configs

                    try:
                        validate_configs(loaded, loaded.cfgs)
                    except SessionConfigError as e:
                        report["error"] = (
                            "; ".join(messages)
                            or f"config did not validate (exit code {e.exit_code})"
                        )
                        report["messages"] = list(messages)
                        report["unknown_keys"] = _unknown_dicts(loaded.unknown_keys)
                        return report
                except (cfgmod.ConfigError, ValueError) as e:
                    # The scratch name is an implementation detail; the caller
                    # asked about their file.
                    report["error"] = str(e).replace(str(tmp), ref or "the config")
                    report["messages"] = list(messages)
                    return report
            report["ok"] = True
            report["messages"] = list(messages)
            report["unknown_keys"] = _unknown_dicts(loaded.unknown_keys)
            report["systems"] = list(loaded.names)
            return report
        finally:
            tmp.unlink(missing_ok=True)

    def _scratch_dir(self, ref: str | None) -> Path:
        if ref is not None:
            directory = self.resolve(ref).parent
        elif self._roots:
            directory = self._roots[0].path
        else:
            raise PathRejected("no config roots are configured")
        if not directory.is_dir():
            raise PathRejected(f"{directory} does not exist")
        return directory

    def write(self, ref: str, text: str) -> dict[str, Any]:
        """Validate, back up whatever is there, then replace it atomically."""
        path = self.resolve(ref)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ConfigTooLarge(f"config is larger than {MAX_BYTES} bytes")
        if not path.parent.is_dir():
            raise PathRejected(f"{ref}: {path.parent} does not exist")
        report = self.validate_text(text, ref)
        if not report["ok"]:
            raise ConfigInvalid(report)
        backup: str | None = None
        if path.exists():
            sibling = path.parent / f".{path.name}.bak"
            try:
                atomic_write_text(sibling, path.read_text(encoding="utf-8"))
                backup = sibling.name
            except OSError:
                log.exception("could not back up %s before overwriting it", path)
        atomic_write_text(path, text)
        log.info("web console: wrote %s (%d bytes)", path, len(text))
        return {
            "ok": True,
            "path": ref,
            "abs_path": str(path),
            "bytes": len(text.encode("utf-8")),
            "backup": backup,
            "unknown_keys": report["unknown_keys"],
            "systems": report["systems"],
        }
