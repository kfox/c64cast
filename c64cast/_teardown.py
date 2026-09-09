"""Run a teardown's steps so a failing one cannot starve the rest.

A teardown's steps are independent promises to whatever runs next, not a
transaction. Sequencing them as bare statements (or inside one ``try``) means
the first raise abandons every promise behind it, and the callers here all sit
under a swallow -- ``Playlist.safe_teardown`` for the scenes -- so the failure
is silent as well.

This lives at the package root, beside :mod:`c64cast._pollthread` and
:mod:`c64cast._midi`, rather than with its first caller in
``scenes/scenes.py``: the audio sources owe the same promises one layer down,
and ``tests/test_audio_source_sid.py``'s ``AudioSourceImportWeightTest`` pins
``audio/audio_source.py`` against dragging in numpy, which importing
``scenes.py`` would do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence


def run_teardown_steps(
    log: logging.Logger,
    who: str,
    steps: Sequence[tuple[str, Callable[[], object]]],
) -> None:
    """Run every teardown step, so a failing one cannot starve the rest.

    Each step is a ``(label, callable)`` pair; a step that raises is logged
    against ``who`` with its label at ERROR, and the run continues. Catches
    ``Exception`` and not ``BaseException`` deliberately: teardown runs on the
    shutdown path, and a ``KeyboardInterrupt`` must propagate rather than be
    logged as a failed step and dropped.

    Guarding a step frees its *position* -- nothing behind it is at risk
    wherever it sits -- with per-caller exceptions that the architecture notes
    name (see ``docs/architecture/scenes.md``)."""
    for what, step in steps:
        try:
            step()
        except Exception:
            log.exception("%s: teardown step %r failed; continuing", who, what)
