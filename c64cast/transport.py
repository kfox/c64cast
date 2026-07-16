"""Live-performance transport + live-tune plumbing.

Phase 1 of the MIDI live-tune feature (see docs/architecture.md → "Live
performance") ships only the pieces that don't need a transport engine yet:

- :func:`atomic_write_text` — the crash-safe "temp file in the same dir +
  ``os.replace``" write, factored out of :class:`wled_device.PresetStore` so the
  loop-preset store (Phase 3) and the config save-back below share one
  implementation instead of duplicating it.
- :class:`LiveTuneTracker` — records every live parameter change a performer
  makes (a knob sweep, a choice cycle) so the exit save-back flow can write the
  final values back into the ``[color]`` section of the run's TOML, or print a
  pasteable snippet for a quick-playback run that has no file.

Later phases add the actual transport session (seek / pause-in-place / A/B loop /
record) to this module. Kept import-light (stdlib only; Config referenced under
TYPE_CHECKING) so it can be pulled in from playlist.py without a cycle.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` to `path` atomically: a temp file in the same directory,
    fsync'd, then ``os.replace``d onto the target (rename is atomic within a
    filesystem), so a crash mid-write can never leave a half-written file. The
    parent directory is created if missing. Shared by PresetStore and the
    live-tune save-back; the loop-preset store (Phase 3) reuses it too."""
    p = os.fspath(path)
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Live-tune targets whose `mode.<field>` name maps back to a field of the same
# (or a renamed) name on the global [color] section. Live tuning drives the
# running DisplayMode; the save-back writes the tuned value into the Config so
# the next run starts there. `dither_method` on the mode is `[color].dither` in
# the config (the config knob also accepts "auto", which the build step resolves
# to a concrete method — writing the concrete method back is intentional: it
# pins what the performer actually dialed in).
#
# `mode.palette_mode` is deliberately absent: palette_mode lives per-scene
# ([[scenes]].palette_mode), not in the shared [color] section, so persisting it
# would need the scene index and is left to a later phase — the live change still
# takes effect at runtime, it just isn't saved.
_MODE_FIELD_TO_COLOR: dict[str, str] = {
    "dither_strength": "dither_strength",
    "motion_smoothing": "motion_smoothing",
    "auto_fit_strength": "auto_fit_strength",
    "dither_method": "dither",
    "cell_strategy": "cell_strategy",
    "color_match": "color_match",
}


class LiveTuneTracker:
    """Records live parameter changes for the exit save-back flow.

    A change is keyed by its live target string (``mode.dither_strength``,
    ``mode.color_match`` …). Re-tuning the same target keeps the ORIGINAL value
    as `old` and overwrites `new`, so what's recorded is the net change from the
    config the run started with — a performer sweeping a knob back and forth ends
    up with a single (old → final) entry, not a churn of intermediates.

    Thread-safe: the MIDI reader thread and the WLED server thread both record;
    the exit flow (main thread) reads. `has_changes` / `describe` / `apply` are
    the read side."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # target -> (old_value, new_value); insertion order preserved.
        self._changes: dict[str, tuple[Any, Any]] = {}

    def record(self, target: str, old: Any, new: Any) -> None:
        """Note that `target` moved from `old` to `new`. No-op when the value
        didn't actually change (a knob landing back where it started clears the
        entry)."""
        with self._lock:
            existing = self._changes.get(target)
            base = existing[0] if existing is not None else old
            if _values_equal(base, new):
                # Back to where it started (or a no-op write) — drop the entry.
                self._changes.pop(target, None)
            else:
                self._changes[target] = (base, new)

    def has_changes(self) -> bool:
        with self._lock:
            return bool(self._changes)

    def describe(self) -> list[str]:
        """Human-readable ``target: old -> new`` lines, for the exit prompt."""
        with self._lock:
            return [f"{t}: {_fmt(o)} -> {_fmt(n)}" for t, (o, n) in self._changes.items()]

    def _persistable(self) -> list[tuple[str, Any]]:
        """(color_field, new_value) pairs for targets that map to [color]."""
        with self._lock:
            items = list(self._changes.items())
        out: list[tuple[str, Any]] = []
        for target, (_, new) in items:
            holder, _, name = target.partition(".")
            if holder != "mode":
                continue
            field = _MODE_FIELD_TO_COLOR.get(name)
            if field is not None:
                out.append((field, new))
        return out

    def apply(self, cfg: Config) -> list[str]:
        """Write the tracked changes into `cfg`'s [color] section (in place).
        Returns ``[color].<field> = <value>`` lines for the ones applied (targets
        that don't map to [color], e.g. palette_mode, are skipped)."""
        applied: list[str] = []
        for field, new in self._persistable():
            setattr(cfg.color, field, new)
            applied.append(f"[color].{field} = {_fmt(new)}")
        return applied

    def toml_snippet(self) -> str:
        """A pasteable ``[color]`` TOML block for the tracked changes — used for
        quick-playback runs that have no config file to write back to. Empty
        string when nothing persistable changed."""
        pairs = self._persistable()
        if not pairs:
            return ""
        # De-dupe (last write wins) while keeping a stable order.
        merged: dict[str, Any] = {}
        for field, new in pairs:
            merged[field] = new
        lines = ["[color]"]
        for field, new in merged.items():
            lines.append(f"{field} = {_toml_value(new)}")
        return "\n".join(lines)


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return a == b
    return a == b


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)
