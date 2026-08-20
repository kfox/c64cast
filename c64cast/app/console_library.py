"""Favorites + recently-launched configs for the web console.

Small enough not to warrant a database and shared enough not to belong in a
single browser's ``localStorage``: the point of a *server* library is that a
phone and a laptop pointed at the same host see the same stars and the same
recent list. Modeled on :class:`c64cast.control.transport.JsonSlotStore`'s
tolerant-load / atomic-write contract, but not a subclass of it — that
contract is for a *numbered-slot* map, and this file's shape is two lists.

Refs, not paths — the same wire identifier
:class:`c64cast.app.config_store.ConfigStore` already uses, so a favorite or a
recent survives being handed straight back to the store with no translation.
A ref that no longer resolves (the file was moved or deleted) is left in
place rather than pruned here: the store, not this module, knows whether a
ref is still good, and a client asking to render one is the one place that
already discovers that.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from c64cast.control.transport import atomic_write_text

from . import paths

log = logging.getLogger(__name__)

#: Bumped only if the on-disk shape ever changes incompatibly. Read
#: tolerantly regardless — see :meth:`ConsoleLibrary._load`.
SCHEMA = 1

#: How many recents to keep. A launch history is for "what was I just
#: working on", not an audit log — the config's own mtime and the session log
#: already answer "when did this last run".
MAX_RECENTS = 20


class ConsoleLibrary:
    """Favorites + recents, persisted to :func:`paths.console_library_path`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else paths.console_library_path()
        # A phone and a laptop can each hit `set_favorite`/`record_recent` in
        # the same moment; both are read-modify-write over one file, and
        # without this a second save can silently overwrite the first's.
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        """Tolerant load: a missing, corrupt, or wrong-shaped file reads as an
        empty library rather than raising, matching `JsonSlotStore`'s contract.
        Only well-formed string entries survive."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"favorites": [], "recents": []}
        if not isinstance(raw, dict):
            return {"favorites": [], "recents": []}
        favorites = [f for f in raw.get("favorites", []) if isinstance(f, str) and f]
        recents = [
            {"ref": r["ref"], "at": r["at"]}
            for r in raw.get("recents", [])
            if isinstance(r, dict)
            and isinstance(r.get("ref"), str)
            and r["ref"]
            and isinstance(r.get("at"), (int, float))
        ]
        return {"favorites": favorites, "recents": recents}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path, json.dumps({"schema": SCHEMA, **data}, indent=2, sort_keys=True)
        )

    def as_dict(self) -> dict[str, Any]:
        return self._load()

    def set_favorite(self, ref: str, on: bool) -> list[str]:
        """Toggle `ref`'s favorite state and return the new favorites list."""
        with self._lock:
            data = self._load()
            favorites = [f for f in data["favorites"] if f != ref]
            if on:
                favorites.append(ref)
            data["favorites"] = favorites
            self._save(data)
            return favorites

    def record_recent(self, ref: str) -> list[dict[str, Any]]:
        """Move `ref` to the front of the recents list (deduplicated), capped
        at :data:`MAX_RECENTS`. Called on every start/switch, from any surface
        — a launch from a MIDI controller or a script counts the same as one
        from the browser."""
        with self._lock:
            data = self._load()
            recents: list[dict[str, Any]] = [r for r in data["recents"] if r["ref"] != ref]
            recents.insert(0, {"ref": ref, "at": time.time()})
            recents = recents[:MAX_RECENTS]
            data["recents"] = recents
            self._save(data)
            return recents
