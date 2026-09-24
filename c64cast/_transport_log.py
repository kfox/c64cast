"""Holding a background poll's HTTP transport records out of ``-vv``.

``-vv`` releases the urllib3 loggers so every REST request to the machine
surfaces, which is what the second ``v`` is for. A steady background read loop
defeats that on its own: the key poller reads ``$028D`` ten times a second for
the whole run, so the handful of records an operator came for arrive buried in
six hundred a minute that report only that the poll is still polling.

:func:`quiet_transport` marks the calling thread's transport records
uninteresting for the duration of a ``with`` block — it is scoped to the block,
not to the thread, so a read on that thread outside the block still shows up.
:class:`QuietTransportFilter` is what acts on the mark; `configure_logging`
attaches it to the transport loggers at ``-vv`` and leaves it off at ``-vvv``,
which is the escape hatch for a run where the poll's own reads *are* the
question. Only DEBUG records are dropped: a urllib3 retry warning raised during
a poll read is evidence, not noise.

See docs/architecture/config.md#_transport_logpy--keeping-a-background-polls-transport-out-of--vv.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator

#: The transport loggers, in one place: `configure_logging` moves their level
#: for `-vv`, and :func:`install` attaches the filter to each. Every logger
#: that raises records of its own has to be named here — a logger filter sees
#: only the records raised on that exact logger, never a descendant's. Record
#: *names* are matched as a whole name or a dotted prefix, so `urllib3x` is
#: out.
TRANSPORT_LOGGERS = ("urllib3", "urllib3.connectionpool")

_local = threading.local()


@contextlib.contextmanager
def quiet_transport() -> Iterator[None]:
    """Mark transport DEBUG records emitted on this thread inside the block as
    chatter. Reentrant, and restores the previous depth on the way out so an
    exception cannot leave a thread permanently quiet."""
    depth: int = getattr(_local, "depth", 0)
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth = depth


def transport_is_quiet() -> bool:
    """Whether the calling thread is inside a :func:`quiet_transport` block."""
    return getattr(_local, "depth", 0) > 0


def _is_transport(name: str) -> bool:
    return any(name == t or name.startswith(f"{t}.") for t in TRANSPORT_LOGGERS)


class QuietTransportFilter(logging.Filter):
    """Drops a transport DEBUG record emitted inside a
    :func:`quiet_transport` block. Everything else passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.levelno <= logging.DEBUG and _is_transport(record.name) and transport_is_quiet()
        )


def install(enabled: bool) -> None:
    """Attach the filter to the transport loggers, or remove it.

    `configure_logging` runs more than once per process — `cli.main` calls it
    on the command line's verbosity and again on the loaded config's — so this
    replaces rather than stacks: a second `-vv` pass would otherwise hang a
    second filter off every transport logger, and a pass that drops to `-vvv`
    or below has to take the first pass's filter back off."""
    for name in TRANSPORT_LOGGERS:
        logger = logging.getLogger(name)
        for f in list(logger.filters):
            if isinstance(f, QuietTransportFilter):
                logger.removeFilter(f)
        if enabled:
            logger.addFilter(QuietTransportFilter())
