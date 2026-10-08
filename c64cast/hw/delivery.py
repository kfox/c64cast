"""Writes a caller needs to know reached the machine.

A backend's ``write_*`` and ``reu_write`` calls return once the transport has
taken the bytes, not once the machine has run them, and a lossy redial can drop
them without an error. ``C64Backend.delivery_epoch`` moves whenever an accepted
write may have been lost, so a write followed by a flush that leaves it unmoved
is known to have landed. `write_confirmed` is that check with a bounded retry,
for the writes whose loss would go unnoticed and stay wrong: a 6502 routine, an
IRQ vector, or REU memory a player reads before anything rewrites it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from c64cast.hw.socket_dma import SocketDMAError

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

# Attempts before a confirmed write gives up.
CONFIRM_TRIES = 3


def write_confirmed(
    api: C64Backend, write: Callable[[], None], *, tries: int = CONFIRM_TRIES
) -> bool:
    """Run ``write`` and flush until a run leaves ``api.delivery_epoch``
    unmoved, at most ``tries`` times. True once one did.

    A run whose ``write`` raises a transport error counts as unconfirmed:
    ``reu_write`` is not routed through ``_emit`` and raises when a redial
    fails or is refused under backoff, which leaves ``delivery_epoch``
    unmoved although nothing was sent.

    A run whose ``write`` already moved the epoch is not flushed: no flush
    can confirm it, and a flush over a link that just refused the write
    logs a warning outside the backend's failure ladder, once per run for a
    caller that retries every second."""
    for _ in range(tries):
        epoch = api.delivery_epoch
        try:
            write()
        except (OSError, SocketDMAError) as e:
            log.debug("confirmed write raised: %s", e)
            continue
        if api.delivery_epoch != epoch:
            continue
        api.flush()
        if api.delivery_epoch == epoch:
            return True
    return False
