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

**A refusal says which file it is about.** Validation runs the whole layered
load, so a stray value in the machine settings refuses every config on the host
with an error naming a section that is nowhere in the file on screen.
:func:`_machine_layer_notes` is the attribution for that case — see its
docstring for the three conditions it insists on before blaming a layer.

**:meth:`ConfigStore.patch` is how the generated form saves**, and it round-trips
through the dataclasses rather than editing text: load, set the named fields,
re-serialize, then hand the result to the same :meth:`ConfigStore.write` a raw
save goes through. Splicing values into the TOML text was the alternative and is
worse in every direction — it needs a writer that understands where a key lives
(and where to put one that isn't there yet), and it can produce a file whose text
no longer means what the form showed. Going through the loader means a form save
is exactly a load-modify-dump, and the round-trip is already property-tested.

What that costs is the file's *prose*: comments and hand-authored layout do not
survive a re-serialize, and a config carrying a secret is refused outright rather
than saved back without it (the serializer never emits ``SECRET_FIELDS``, so a
round-trip would silently drop a password the operator put there). Both are why
the raw text editor stays the primary surface and the form is the convenience —
and why the replaced text is on disk as a sibling before the new one lands.

What is written is the config the loader *resolved*, which includes the
machine-settings layer — so every comparison this module makes against "unset"
is made against :func:`config.machine_baseline`, not a blank ``Config()``.
That is one decision with three faces. :func:`describe` reports ``is_default``
against it, so the form's "only what this file changes" means the file and not
the machine. ``reset`` on an edit puts a field *back* to it, which is how a form
removes a key rather than pinning a shipped default over a machine setting. And
``dumps`` measures against it, so a capture device set once on this machine is
not written into every show config saved from it (and then carried to another
machine, where it would override that machine's own). The same baseline is what
the secret check compares against, so a ``dma_password`` living in the machine
settings — where it is legal — doesn't read as one this file carries.

What a ref bounds is *which files are edited*, not what a config can then
reach: a saved TOML names media paths and URLs that a session will open. Remote
config write access is equivalent to local shell-ish reach, which is why the
full token gates it and why the viewer role can't write at all.
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from c64cast.control.transport import atomic_write_text

from . import config as cfgmod
from . import config_serialize, introspect, paths, wizard
from .fs_walk import MAX_FILES, disambiguate, walk_dirs

log = logging.getLogger(__name__)

#: The only extension the browser will read or write. A config console that can
#: open arbitrary files is a file manager, which is a much larger promise.
SUFFIX = ".toml"

#: Cap on a single file's size. Same rationale as `fs_walk.MAX_FILES`/
#: `MAX_DEPTH`: keeping a hostile or merely enormous file from turning one
#: request into minutes of I/O, not a security boundary.
MAX_BYTES = 1 << 20


class ConfigStoreError(Exception):
    """Base for every refusal from this module. Messages are end-user readable."""


class PathRejected(ConfigStoreError):
    """The ref names nothing this store is willing to touch."""


class ConfigNotFound(ConfigStoreError):
    """The ref is legal but there is no such file."""


class ConfigTooLarge(ConfigStoreError):
    """Past :data:`MAX_BYTES` — refused rather than read into memory."""


class EditRejected(ConfigStoreError):
    """A form edit named something this store won't set. Distinct from
    :class:`ConfigInvalid`, which is a legal edit whose *result* doesn't run."""


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
    #: True for the packaged examples root — readable and copyable, never
    #: written to. See :meth:`ConfigStore._require_writable`.
    readonly: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "path": str(self.path), "readonly": self.readonly}


def _label_for(path: Path, taken: set[str]) -> str:
    label, _ = disambiguate(path.name or "root", "", taken.__contains__)
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


def _unplayable_warning(detail: str) -> dict[str, Any]:
    """A scene with no media chosen yet, carried as a warning instead of the
    refusal it is during a run.

    Only reachable from a structured edit — see
    :meth:`ConfigStore.validate_text`. Same shape as a media warning so the
    console renders it in the list it already has, with no scene to point at:
    the pre-flight stops at the first scene that fails and speaks about the
    show, not about one field."""
    return {
        "system": "",
        "scene": None,
        "field": None,
        "detail": f"saved, but this will not start until it names its media: {detail}",
    }


def _media_warnings(cfgs: Sequence[cfgmod.Config], names: Sequence[str]) -> list[dict[str, Any]]:
    """Scenes whose `file =` names local media that isn't there.

    A warning rather than a refusal, because :func:`scene_factory.missing_media`
    is reporting exactly the case the loader lets through on purpose — a path
    that will exist by showtime, or one that names media on another machine in
    an ensemble. Reported in the same report the console already renders, so
    the answer arrives before the C64 is opened and reset rather than seconds
    into the run."""
    from .scene_factory import missing_media

    out: list[dict[str, Any]] = []
    for cfg, system in zip(cfgs, list(names) + [""] * len(cfgs), strict=False):
        for index, scene in enumerate(cfg.scenes):
            for entry in missing_media(scene.file or ""):
                out.append(
                    {
                        "system": system,
                        "scene": index,
                        "field": "file",
                        "detail": (
                            f"scene {index + 1} ({scene.type}) names {entry!r}, "
                            "which is not on this host — the scene will fail when it "
                            "starts unless the file is there by then."
                        ),
                    }
                )
    return out


def _machine_layer_notes(text: str, blame: str) -> list[dict[str, Any]]:
    """Machine settings that `text` does not set and that `blame` names.

    A config file is validated with the machine-settings layer under it, so a
    stray value in ``~/.config/c64cast/settings.toml`` makes *every* config on
    this host refuse to save — with an error naming a section, and nothing
    saying the value is not in the file on screen. The reflex is to hunt for a
    key in a file that does not contain it.

    This is the attribution, done structurally rather than by re-loading: a key
    the machine layer supplies, the edited text is silent about, and the failure
    mentions by name. All three have to hold, so a machine setting the file
    overrides is never blamed and neither is one the failure never mentioned —
    a wrong pointer is worse than none."""
    try:
        machine = cfgmod.load_machine_settings()
    except cfgmod.ConfigError as e:
        # The settings file itself won't parse. That is worth saying outright:
        # nothing on this host will load until it is fixed.
        return [{"path": str(paths.settings_path()), "section": "", "key": "", "error": str(e)}]
    if not machine:
        return []
    try:
        own = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        own = {}
    notes: list[dict[str, Any]] = []
    for section, values in machine.items():
        if not isinstance(values, dict):
            continue
        here = own.get(section)
        for key, value in values.items():
            if isinstance(here, dict) and key in here:
                continue
            if key not in blame:
                continue
            notes.append(
                {
                    "path": str(paths.settings_path()),
                    "section": section,
                    "key": key,
                    "value": _value(value),
                    "error": None,
                }
            )
    return notes


def describe(cfg: cfgmod.Config, baseline: cfgmod.Config | None = None) -> dict[str, Any]:
    """The loaded config as form data: every field's value, its ``baseline``,
    and whether the two agree (``is_default``).

    ``is_default`` is the same comparison ``config_serialize._should_emit``
    makes when deciding whether a field is worth writing, which is what lets a
    UI offer "show only what I've changed" without the server deciding for it —
    so it must be measured against the same ``baseline`` the save will use, or
    the form marks a field the file does not contain. None means the dataclass
    defaults; :meth:`ConfigStore.read` passes the machine baseline.

    ``baseline`` is sent per field rather than left to the client's copy of the
    introspection document, which carries the *dataclass* default and would
    therefore promise the wrong thing on a machine whose settings say otherwise:
    it is what a ``reset`` edit will actually leave behind, so a form can say so
    before asking for one.

    The scene field lists come from ``introspect`` already filtered by
    ``applies_to``, so a scene's form can't offer a knob its type ignores."""
    blank = baseline if baseline is not None else cfgmod.Config()
    sections: list[dict[str, Any]] = []
    for sd in introspect.config_sections():
        section = getattr(cfg, sd.name)
        default_section = getattr(blank, sd.name)
        fields: list[dict[str, Any]] = []
        for fd in sd.fields:
            if (sd.name, fd.name) in config_serialize.SECRET_FIELDS:
                continue
            value = getattr(section, fd.name)
            default = getattr(default_section, fd.name)
            fields.append(
                {
                    "name": fd.name,
                    "value": _value(value),
                    "baseline": _value(default),
                    "is_default": value == default,
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
            default = getattr(blank_scene, fd.name)
            fields.append(
                {
                    "name": fd.name,
                    "value": _value(value),
                    "baseline": _value(default),
                    "is_default": value == default,
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


def _editable_fields() -> dict[str, frozenset[str]]:
    """Section name -> the fields a form edit may set, straight from
    ``introspect`` so the editable surface is the one ``describe`` renders."""
    return {
        sd.name: frozenset(
            fd.name for fd in sd.fields if (sd.name, fd.name) not in config_serialize.SECRET_FIELDS
        )
        for sd in introspect.config_sections()
    }


def _editable_scene_fields(scene_type: str) -> frozenset[str]:
    for st in introspect.scene_types():
        if st.name == scene_type:
            # `overlays` is in the form as its own list rather than a field, so
            # `describe` drops it from the field list — but it is still a scene
            # field an editor can replace wholesale.
            #
            # `type` is *not* editable, and it is the one field that has to be
            # named to say so. It decides which of the other fields mean
            # anything, so changing it here doesn't edit the scene — it
            # reinterprets it, and the re-serialize then drops every field the
            # new type has no use for. That is a structural change and belongs
            # with the text editor, next to adding and removing scenes.
            return frozenset({fd.name for fd in st.fields} | {"overlays"}) - {"type"}
    raise EditRejected(f"unknown scene type {scene_type!r}")


def _schema_directive(text: str) -> str | None:
    """The file's own ``#:schema`` line, or None. Kept rather than regenerated:
    a config pinned to a local schema path should stay pinned to it, and one
    that never had the directive shouldn't grow one from being edited."""
    lines = text.lstrip().splitlines()
    if lines and lines[0].startswith("#:schema "):
        return lines[0][len("#:schema ") :].strip() or None
    return None


def _require_scene_index(scenes: Sequence[cfgmod.SceneCfg], index: int, verb: str) -> None:
    """Raise :class:`EditRejected` unless ``index`` names a scene in ``scenes``.

    Shared by every place a request names a scene by index — a field edit, a
    copy source, a removal, a reorder's `index` and its `to` — so the bounds
    check and its wording can't drift between them. ``verb`` is the part of
    the message specific to why the index was needed, e.g. ``"no scene at
    index"`` or ``"cannot move to index"``."""
    if not 0 <= index < len(scenes):
        raise EditRejected(f"{verb} {index} (the config has {len(scenes)})")


def _apply_edit(cfg: cfgmod.Config, edit: object, baseline: cfgmod.Config) -> dict[str, Any]:
    """Set one field on a loaded config, and describe what was set.

    ``baseline`` is what ``reset`` puts a field back to. The machine-overlaid
    Config rather than a blank one: reset means "this file stops saying
    anything about this field", and what shows through then is whatever the
    layer below already said."""
    if not isinstance(edit, Mapping):
        raise EditRejected(f"an edit is an object, got {type(edit).__name__}")
    field = str(edit.get("field", "")).strip()
    if not field:
        raise EditRejected("an edit needs a `field`")
    section = edit.get("section")
    scene = edit.get("scene")
    if (section is None) == (scene is None):
        raise EditRejected(f"{field}: an edit names either a `section` or a `scene`, not both")

    if section is not None:
        allowed = _editable_fields().get(str(section))
        if allowed is None:
            raise EditRejected(f"[{section}] is not a config section")
        if field not in allowed:
            raise EditRejected(f"[{section}] has no editable field {field!r}")
        target: Any = getattr(cfg, str(section))
        blank: Any = getattr(baseline, str(section))
        where: dict[str, Any] = {"section": str(section)}
    else:
        if not isinstance(scene, int) or isinstance(scene, bool):
            raise EditRejected(f"a scene is named by its index, got {scene!r}")
        _require_scene_index(cfg.scenes, scene, "no scene at index")
        target = cfg.scenes[scene]
        if field == "type":
            raise EditRejected(
                "a scene's `type` decides what its other fields mean, so changing it "
                "rewrites the block rather than editing it — edit this file as text."
            )
        if field not in _editable_scene_fields(target.type):
            raise EditRejected(f"a {target.type!r} scene has no editable field {field!r}")
        blank = cfgmod.SceneCfg()
        where = {"scene": scene}

    if edit.get("reset"):
        value = deepcopy(getattr(blank, field))
    elif "value" in edit:
        value = edit["value"]
    else:
        raise EditRejected(f"{field}: an edit needs a `value`, or `reset = true`")
    setattr(target, field, value)
    return {**where, "field": field, "value": _value(value)}


class ConfigStore:
    """The browser's view of the filesystem: a few named roots and nothing else.

    ``roots`` are the configured directories (``[web].config_roots``); an empty
    list means the working directory, which is where a ``c64cast --serve`` run
    launched from a show folder already has its configs. A root that doesn't
    exist is dropped with a warning rather than failing the host — the console
    is still useful for starting the config it was launched with.

    A trailing, read-only root for the packaged examples is appended
    automatically (``include_examples=False`` turns that off — mainly so a
    test fixture built around a single configured root doesn't also have to
    account for whatever ships in ``c64cast/examples/``)."""

    def __init__(
        self,
        roots: Sequence[str] = (),
        *,
        cwd: Path | None = None,
        include_examples: bool = True,
    ) -> None:
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
        if include_examples:
            self._append_examples_root(resolved, taken)
        self._roots = tuple(resolved)
        self._by_label = {r.label: r for r in self._roots}
        self._ref_for_cache: tuple[str, str | None] | None = None

    @staticmethod
    def _append_examples_root(resolved: list[Root], taken: set[str]) -> None:
        """Add the packaged examples as a trailing, read-only root, so they are
        listed and readable (and thus copyable — see :meth:`create`) without
        being mistaken for a place a user's own config can be saved.

        Best-effort: a zipapp install has no real example files on disk (see
        :func:`paths._package_dir`), and that is not a reason for the console
        to refuse to start."""
        try:
            path = paths.examples_dir().resolve()
        except RuntimeError:
            return
        if not path.is_dir() or any(path == r.path for r in resolved):
            return
        label = _label_for(path, taken)
        taken.add(label)
        resolved.append(Root(label=label, path=path, readonly=True))

    @property
    def roots(self) -> tuple[Root, ...]:
        return self._roots

    # -- refs ---------------------------------------------------------------

    @staticmethod
    def _ref_parts(ref: str) -> list[str]:
        """Split a wire ref into its non-empty, non-`.` segments.

        Shared by :meth:`resolve` and :meth:`_require_writable` so a leading
        `/` or `./` (or a doubled slash) is filtered out the same way in both
        places — re-deriving the root label with a naive `split("/", 1)` let
        such a ref name a root :meth:`resolve` had correctly found, while the
        write-side readonly check looked up an empty label and skipped
        itself."""
        return [p for p in str(ref).replace("\\", "/").split("/") if p not in ("", ".")]

    def resolve(self, ref: str) -> Path:
        """Turn a wire ref into an absolute path inside a root, or refuse.

        The returned path need not exist — a write to a new file is legal — but
        it is always a real location under a root, symlinks followed."""
        parts = self._ref_parts(ref)
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
        """The ref that addresses `path`, or None if no root contains it.

        Cached on the input path's string form: a websocket status frame
        calls this every ~0.35s per connected client, but `config_path` only
        changes on start/switch, so most calls would otherwise pay for a
        `Path.resolve()` stat to re-derive the same answer."""
        key = str(path)
        cached = self._ref_for_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        resolved = Path(path).resolve()
        ref = None
        for root in self._roots:
            if resolved.is_relative_to(root.path):
                rel = resolved.relative_to(root.path)
                ref = "/".join((root.label, *rel.parts))
                break
        self._ref_for_cache = (key, ref)
        return ref

    def _require_writable(self, ref: str) -> Path:
        """:meth:`resolve`, plus a refusal for the packaged examples root.

        A ref's own label says which root it is in, so this needs no second
        walk of ``self._roots`` — the same label :meth:`resolve` just checked
        is looked up again here. Copy an example (:meth:`create` with
        ``copy_of``) to get an editable file from one."""
        path = self.resolve(ref)
        label = self._ref_parts(ref)[0]
        root = self._by_label.get(label)
        if root is not None and root.readonly:
            raise PathRejected(f"{ref!r} is a read-only example — duplicate it to edit")
        return path

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
                        "rel": rel.as_posix(),
                        "name": path.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "readonly": root.readonly,
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
        for here, filenames in walk_dirs(root.path):
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
        out["form"] = describe(loaded.cfgs[0], cfgmod.machine_baseline())
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

    def validate_text(
        self, text: str, ref: str | None = None, *, partial: bool = False
    ) -> dict[str, Any]:
        """Load `text` as if it were saved, without saving it.

        Thin wrapper over :meth:`_validate_text_and_load`, for callers (every
        caller but :meth:`validate_ref`) that only want the report."""
        report, _loaded = self._validate_text_and_load(text, ref, partial=partial)
        return report

    def _validate_text_and_load(
        self,
        text: str,
        ref: str | None = None,
        *,
        partial: bool = False,
        _load_path: Path | None = None,
    ) -> tuple[dict[str, Any], cfgmod.LoadResult | None]:
        """Do :meth:`validate_text`'s work, and also hand back the
        ``LoadResult`` on success — so :meth:`validate_ref` can feed it
        straight to the doctor pass instead of loading the same file twice.

        `_load_path`, ``validate_ref``-only, is the real file `text` was just
        read from. Loading it directly (instead of a scratch copy of `text`)
        gets a `LoadResult` whose `paths` are the real, still-there files the
        doctor pass reads again for its schema-directive check — a scratch
        copy's path is a tempfile this method deletes before that pass runs.
        Skipped by every other caller, whose `text` may be unsaved edits that
        don't match `_load_path`'s (or any) file on disk.

        The scratch file goes in the *target's own directory* rather than a temp
        dir: an ensemble master resolves its per-system paths relative to
        itself, so validating one anywhere else would report missing files that
        are not missing.

        `partial` says this text is a show part-way through being built rather
        than a finished statement about one, and excuses exactly one refusal:
        :class:`scene_factory.MediaNotChosen`, a scene that names no media on a
        host with none to default to. That is the state every scene is in the
        instant the console adds it, so refusing it makes the first step of
        building a show impossible — and the refusal would name `assets/videos`
        while the button said *add a scene*. It comes back as a warning in the
        same report instead. Every other failure still refuses, including a bad
        value the form itself produced: that one is wrong now and wrong later,
        and the save is the last chance to say so."""
        report: dict[str, Any] = {
            "ok": False,
            "error": None,
            "messages": [],
            "unknown_keys": [],
            "systems": [],
            # Things that load but will bite. Only filled on success: a config
            # that doesn't load has no scenes to look at.
            "warnings": [],
            # Filled only on a failure this file may not be responsible for —
            # see _machine_layer_notes.
            "layers": [],
            # Only ever populated by validate_ref's pre-flight — a check of
            # unsaved text has no file on disk for the doctor to look at.
            "diagnostics": [],
        }
        tmp: Path | None = None
        if _load_path is None:
            directory = self._scratch_dir(ref)
            try:
                fd, tmp_name = tempfile.mkstemp(
                    prefix=".c64cast-check-", suffix=SUFFIX, dir=directory
                )
            except OSError as e:
                # `directory` is the target's own (possibly read-only-by-policy,
                # or on a wheel install genuinely unwritable) directory — see the
                # docstring for why it has to be that one. A denied write belongs
                # in the report, not an unhandled 500.
                raise PathRejected(f"cannot check a config in {directory}: {e}") from e
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        load_path = tmp if _load_path is None else _load_path
        unplayable: list[dict[str, Any]] = []
        try:
            with _capture_errors() as messages:
                try:
                    loaded = cfgmod.load_master(str(load_path))
                    from .scene_factory import MediaNotChosen
                    from .session import SessionConfigError, validate_configs

                    try:
                        validate_configs(loaded, loaded.cfgs)
                    except SessionConfigError as e:
                        detail = (
                            "; ".join(messages)
                            or f"config did not validate (exit code {e.exit_code})"
                        )
                        # `from e` all the way down, so the pre-flight's own
                        # cause is the question — asked of the type rather than
                        # of the prose, which is a user-facing string and moves.
                        if partial and isinstance(e.__cause__, MediaNotChosen):
                            unplayable = [_unplayable_warning(detail)]
                        else:
                            report["error"] = detail
                            report["messages"] = list(messages)
                            report["unknown_keys"] = _unknown_dicts(loaded.unknown_keys)
                            # The file itself parsed fine — validate_configs
                            # is what refused — so the doctor pass still has
                            # something to look at.
                            return self._blame_layers(report, text), loaded
                except (cfgmod.ConfigError, ValueError) as e:
                    # The scratch name is an implementation detail; the caller
                    # asked about their file.
                    report["error"] = str(e).replace(str(load_path), ref or "the config")
                    report["messages"] = list(messages)
                    return self._blame_layers(report, text), None
            report["ok"] = True
            report["messages"] = list(messages)
            report["unknown_keys"] = _unknown_dicts(loaded.unknown_keys)
            report["systems"] = list(loaded.names)
            report["warnings"] = unplayable + _media_warnings(loaded.cfgs, loaded.names)
            return report, loaded
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def validate_ref(self, ref: str) -> dict[str, Any]:
        """Validate the file as it stands on disk, plus every *other* problem
        in it — the console's pre-flight before a start.

        ``validate_text`` alone is fail-fast, the same question
        ``validate_configs`` asks before a start: it stops at the first
        problem. This adds ``doctor.validate_load_result``'s collect-all pass
        (``probe_u64=False`` keeps it network-free, and it has never been
        reachable over HTTP before) as a ``diagnostics`` list, so a bad
        config names everything wrong with it at once instead of one thing
        per click. Diagnostics run regardless of ``ok`` — a config that fails
        the fail-fast check but still loads gets the full list too — and stay
        empty when the file doesn't even load, since there's nothing loaded
        for the doctor to look at."""
        path = self.resolve(ref)
        report, loaded = self._validate_text_and_load(self._read_text(path), ref, _load_path=path)
        if loaded is None:
            return report
        from .doctor import validate_load_result

        report["diagnostics"] = [
            asdict(d)
            for d in validate_load_result(loaded, probe_u64=False, probe_environment=False)
        ]
        return report

    @staticmethod
    def _blame_layers(report: dict[str, Any], text: str) -> dict[str, Any]:
        """Point a failed report at the layer under the file, when there is one
        to point at. A no-op on the usual failure, where the file itself is
        wrong."""
        blame = " ".join([str(report["error"] or ""), *report["messages"]])
        report["layers"] = _machine_layer_notes(text, blame)
        return report

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

    def write(self, ref: str, text: str, *, partial: bool = False) -> dict[str, Any]:
        """Validate, back up whatever is there, then replace it atomically.

        `partial` is :meth:`validate_text`'s, and reaches it unchanged."""
        path = self._require_writable(ref)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ConfigTooLarge(f"config is larger than {MAX_BYTES} bytes")
        if not path.parent.is_dir():
            raise PathRejected(f"{ref}: {path.parent} does not exist")
        report = self.validate_text(text, ref, partial=partial)
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
            # Carried through a save as well as a check: the answer to "will
            # this run?" is the same either way, and a save is the moment
            # somebody stops looking at the check.
            "warnings": report["warnings"],
        }

    def patch(self, ref: str, edits: Sequence[Any]) -> dict[str, Any]:
        """Set fields on an existing config and write it back.

        Each edit names a ``section`` (or a ``scene`` index) and a ``field``,
        plus either a ``value`` or ``reset = true`` to put the field back to the
        baseline — the only way a form can *remove* a key, and the inverse of
        the ``is_default`` flag :func:`describe` reports.

        Fields come from ``introspect``, so an edit can only reach what the form
        actually rendered: a scene's own type's fields, never another type's.
        *Which* scenes exist is not an edit — see :meth:`add_scene` and
        :meth:`remove_scene`, which change the shape of the file rather than the
        value of a field, and are the two structural moves worth a button.

        Everything after the last edit is :meth:`write`: the result is validated
        as a whole, the previous text is kept as a sibling, and a config that no
        longer runs is refused with the file untouched."""
        out = self._rewrite(
            ref, lambda cfg, baseline: [_apply_edit(cfg, e, baseline) for e in edits]
        )
        out["edits"] = out.pop("result")
        return out

    def add_scene(
        self,
        ref: str,
        *,
        scene_type: str = "",
        copy_of: int | None = None,
        after: int | None = None,
    ) -> dict[str, Any]:
        """Append or insert a scene, and write the file back.

        Either a blank scene of ``scene_type`` or a copy of the scene at
        ``copy_of`` — "add another clip like that one" is the common ask, and
        re-typing a dozen fields to get it is what sent people back to the text
        editor. ``after`` is the index to insert behind; ``None`` appends.

        A copy is taken verbatim, name included. Inventing "name (copy)" would
        be guessing at what the show should call it, and a duplicate name is
        visible in the very list this was reached from."""
        if (scene_type == "") == (copy_of is None):
            raise EditRejected("adding a scene names either a `type` or a `copy` index, not both")
        known = {st.name for st in introspect.scene_types()}
        if scene_type and scene_type not in known:
            raise EditRejected(f"unknown scene type {scene_type!r}; known: {sorted(known)}")

        def mutate(cfg: cfgmod.Config, _baseline: cfgmod.Config) -> dict[str, Any]:
            if copy_of is not None:
                _require_scene_index(cfg.scenes, copy_of, "no scene at index")
            if after is not None and not -1 <= after < len(cfg.scenes):
                raise EditRejected(f"cannot insert after index {after}")
            scene = (
                deepcopy(cfg.scenes[copy_of])
                if copy_of is not None
                else cfgmod.SceneCfg(type=scene_type)
            )
            at = len(cfg.scenes) if after is None else after + 1
            cfg.scenes.insert(at, scene)
            return {"added": at, "type": scene.type, "copied_from": copy_of}

        out = self._rewrite(ref, mutate)
        out["scene"] = out.pop("result")
        return out

    def remove_scene(self, ref: str, index: int) -> dict[str, Any]:
        """Drop a scene and write the file back.

        The pair to :meth:`add_scene`: a console that can add and not remove is
        a one-way door, and the way back would be the text editor this exists to
        avoid. The last scene stays — a playlist with nothing in it is not a
        show, and the loader would refuse the write anyway with a message about
        scenes rather than about the button that was pressed."""

        def mutate(cfg: cfgmod.Config, _baseline: cfgmod.Config) -> dict[str, Any]:
            _require_scene_index(cfg.scenes, index, "no scene at index")
            if len(cfg.scenes) == 1:
                raise EditRejected("this is the only scene — a show needs one to play")
            gone = cfg.scenes.pop(index)
            return {"removed": index, "type": gone.type, "name": gone.name}

        out = self._rewrite(ref, mutate)
        out["scene"] = out.pop("result")
        return out

    def move_scene(self, ref: str, index: int, to: int) -> dict[str, Any]:
        """Reorder a scene and write the file back.

        The one structural move `add_scene`/`remove_scene` never got a route
        for — the order of a show was otherwise a text-editor job. A no-op
        move (``index == to``) is accepted and idempotent, the same
        tolerance its neighbors have for a request that changes nothing."""

        def mutate(cfg: cfgmod.Config, _baseline: cfgmod.Config) -> dict[str, Any]:
            _require_scene_index(cfg.scenes, index, "no scene at index")
            _require_scene_index(cfg.scenes, to, "cannot move to index")
            scene = cfg.scenes.pop(index)
            cfg.scenes.insert(to, scene)
            return {"moved": index, "to": to, "type": scene.type, "name": scene.name}

        out = self._rewrite(ref, mutate)
        out["scene"] = out.pop("result")
        return out

    def create(self, ref: str, *, copy_of: str | None = None) -> dict[str, Any]:
        """Make a new config at `ref`.

        `copy_of` is any *readable* ref — including one under the read-only
        examples root, which is how an example becomes an editable starting
        point rather than something only ``--config example:NAME`` can reach.
        With no `copy_of`, the new file is a minimal single-scene starter
        (a ``blank`` scene, the one type with nothing to point at a file for),
        built the same way ``--init`` builds one: on top of
        :func:`c64cast.app.config.machine_baseline`, so a machine setting this
        host already carries isn't written into the new file as if the show
        chose it. Audio starts disabled, mirroring the interactive wizard's own
        default answer to "enable SID audio streaming?" — a brand-new config
        should validate on a host without the optional `mic` extra, not fail
        before a single scene has been added.

        Refuses an existing path outright — this creates, it does not save —
        and refuses a parent directory that doesn't exist yet rather than
        guessing at a new one to make."""
        path = self._require_writable(ref)
        if path.exists():
            raise PathRejected(f"{ref} already exists")
        if not path.parent.is_dir():
            raise PathRejected(f"{ref}: {path.parent} does not exist")
        if copy_of is not None:
            text = self._read_text(self.resolve(copy_of))
        else:
            # Two separate calls, not one shared instance: `base` is mutated
            # in place by build_multi_config, and dumps() needs an untouched
            # baseline to diff against — mirrors wizard.py's _run_single/_run_multi.
            baseline = cfgmod.machine_baseline()
            cfg = wizard.build_multi_config(
                scenes=[cfgmod.SceneCfg(type="blank")],
                base=cfgmod.machine_baseline(),
                audio_enabled=False,
            )
            text = config_serialize.dumps(cfg, baseline=baseline)
        out = self.write(ref, text, partial=True)
        out["created"] = True
        out["copied_from"] = copy_of
        return out

    def delete(self, ref: str) -> dict[str, Any]:
        """Remove a config file.

        Refuses a read-only root the same way a write does. Refusing the
        config a session is currently running is left to the caller — this
        store has no notion of a running session, and `web_api` does."""
        path = self._require_writable(ref)
        if not path.is_file():
            raise ConfigNotFound(f"no such config: {ref}")
        path.unlink()
        log.info("web console: deleted %s", path)
        return {"ok": True, "path": ref}

    def _rewrite(
        self, ref: str, mutate: Callable[[cfgmod.Config, cfgmod.Config], Any]
    ) -> dict[str, Any]:
        """Load ``ref``, let ``mutate`` change the loaded Config, and write the
        re-serialized result back.

        The shared spine of every structured write. ``mutate`` is handed the
        config and the machine baseline and returns whatever the caller wants
        reported, which arrives as ``result``. Everything around it — the
        refusals below, the re-serialize, and :meth:`write`'s validate-then-back-
        up-then-replace — is the same for a field edit and for a scene added or
        removed, and having it in one place is what keeps them that way.

        Writes `partial`: every edit that arrives here is one step of building a
        show, so a scene that has not named its media yet is a warning rather
        than a refusal. See :meth:`validate_text` for what that does and does
        not excuse."""
        path = self._require_writable(ref)
        original = self._read_text(path)
        try:
            loaded = cfgmod.load_master(str(path))
        except (cfgmod.ConfigError, ValueError) as e:
            raise EditRejected(
                f"{ref} does not load, so the form has nothing to edit — fix it as text first: {e}"
            ) from e
        if loaded.is_ensemble:
            raise EditRejected(
                f"{ref} is an ensemble master; those are authored as text (the "
                "serializer refuses them by design). Edit the per-system configs."
            )
        cfg = loaded.cfgs[0]
        # Measured against the machine layer, not a blank Config: a password in
        # the machine-settings file is legal and is not something *this* file
        # carries, so it must not block editing this file.
        baseline = cfgmod.machine_baseline()
        for section, name in sorted(config_serialize.SECRET_FIELDS):
            if getattr(getattr(cfg, section), name) != getattr(getattr(baseline, section), name):
                raise EditRejected(
                    f"{ref} carries [{section}].{name}, which is never written back — "
                    "saving the form would drop it. Edit this file as text, or move "
                    "the secret to its environment variable."
                )

        result = mutate(cfg, baseline)
        try:
            text = config_serialize.dumps(
                cfg,
                annotate=False,
                minimal=True,
                schema_path=_schema_directive(original),
                baseline=baseline,
            )
        except config_serialize.SerializeError as e:
            raise EditRejected(f"{ref} can't be written back: {e}") from e
        out = self.write(ref, text, partial=True)
        out["result"] = result
        out["text"] = text
        return out
