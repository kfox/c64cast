"""Taking a raster IRQ handler off `$0314` over a link that can lose writes.

Three callers unhook one: a bitmap mode's bank-swap teardown
(`video/modes_irq.uninstall_bank_swap_irq`), `big_text`'s teardown, and the
interstitial card, which defeats a handler a teardown leaked. They used to
carry a sequence each, with retry rules of their own, and the copies drifted:
one restored `$0314` only once its raster disable landed, another restored it
regardless and wrote the disable again behind it. `unhook_raster_irq` is the
one sequence all three run.

Not every step is an independent promise, so the order is the contract:

* **Both sources are masked first** (CIA #1, then the VIC raster source), so
  nothing enters the handler while it is being taken away. A REU dispatcher
  already inside a copy keeps running from `$C500` after the masks land, so a
  caller whose handler copies passes a `drain` that waits it out.
* **The restore runs whether or not the masks confirmed.** The 6510 reads
  `$0314` only on IRQ entry, so a restore landing mid-handler is harmless,
  while a handler left hooked stays reachable while the next scene's setup
  writes over it. A mask that did not confirm is written again behind the
  restore: the kernal handler at `$EA31` never acks `$D019`, so a raster source
  left live re-enters the IRQ on every RTI. The CIA #1 mask is retried only
  while `$0314` is still hooked, since the unmask re-arms Timer A otherwise.
* **The drain is repeated** when a mask never confirmed but the restore did (an
  IRQ up to the restore could have started a copy the first drain never saw),
  and when the restore failed but a retry masked the last live source (the
  handler is out of reach from then on, and a copy it started may still run).
* **An unconfirmed restore is read back** once the link answers, since a redial
  during it moves the loss mark whether or not the write landed.
* **CIA #1 is unmasked only once the restore landed.** With `$0314` still on
  the handler, every jiffy IRQ vectors through RAM the next scene writes over;
  a masked Timer A costs the keyboard scan, an unmasked one the machine.

Every mask, the restore and the unmask go through
`hw/delivery.write_confirmed`, because a lost write moves the backend's loss
mark without raising. Each step runs under `run_teardown_steps`, so a raise in
one cannot starve the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from c64cast._teardown import run_teardown_steps
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA1, CIA2, KERNAL, VECTORS
from c64cast.hw.delivery import CONFIRM_TRIES, write_confirmed

CIA1_MASK = "CIA1 mask"
RASTER_DISABLE = "VIC IRQ disable"

_KERNAL_VECTOR_BYTES = bytes([KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF])

_MASKS = {
    CIA1_MASK: (f"{CIA1.ICR:04X}", f"{CIA1.ICR_DISABLE_ALL:02X}"),
    RASTER_DISABLE: ("D01A", "00"),
}


def confirm(api: C64Backend, what: str, write: Callable[[], None]) -> None:
    """Run `write` under `write_confirmed`, raising when it never confirms so
    `run_teardown_steps` logs the step against its label."""
    if not write_confirmed(api, write):
        raise RuntimeError(f"the {what} write was not confirmed after {CONFIRM_TRIES} tries")


def _vector_reads_back_kernal(api: C64Backend, log: logging.Logger, who: str) -> bool:
    """Whether `$0314/$0315` reads back as `$EA31`, asked once the link answers.

    An unconfirmed restore is not always a lost one: a redial during it moves
    the loss mark although the write may have landed, and counting it as lost
    leaves CIA #1 masked, and the keyboard dead, with the vector already on the
    kernal. `link_answers` comes first, so the read runs only over a link that
    carries it and behind every write sent before it. A read that cannot be
    made, or fails, answers False: the restore stays unconfirmed and CIA #1
    masked."""
    try:
        if not api.link_answers():
            return False
        got = api.read_memory(VECTORS.IRQ, 2)
    except Exception as e:
        # BackendCapabilityError on a backend with no reads, or the transport's own.
        log.debug("%s: reading back $0314 failed: %s", who, e)
        return False
    if got != _KERNAL_VECTOR_BYTES:
        return False
    log.warning("%s: the $0314 restore went unconfirmed but reads back as $EA31", who)
    return True


def unhook_raster_irq(
    api: C64Backend,
    log: logging.Logger,
    who: str,
    *,
    drain: Callable[[], None] | None = None,
    before_unmask: Sequence[tuple[str, Callable[[], object]]] = (),
    if_still_hooked: Callable[[], None] | None = None,
) -> bool:
    """Mask both IRQ sources, put `$0314` back on `$EA31`, ack `$D019`, run
    `before_unmask`, and unmask CIA #1 Timer A once the restore landed.

    `log` and `who` name the caller in what is logged. `drain` waits out a
    copy the handler may have in flight; it runs after the masks, and again
    wherever the module docstring says a copy could have started since.
    `before_unmask` holds the caller's own steps, run behind the ack and ahead
    of the unmask (the bank-swap callers pin VIC bank 0 there). `if_still_hooked`
    runs last, only when the restore did not land, for whatever makes a handler
    left on `$0314` harmless.

    Returns whether the restore landed."""
    vector_restored = False
    unconfirmed_masks: list[str] = []
    late_mask_landed = False

    def mask(what: str) -> None:
        address, value = _MASKS[what]
        # Listed first, so a write that raises past `confirm` counts as unconfirmed.
        unconfirmed_masks.append(what)
        confirm(api, what, lambda: api.write_memory(address, value))
        unconfirmed_masks.remove(what)

    def mask_again(what: str) -> None:
        nonlocal late_mask_landed
        if what not in unconfirmed_masks or (what == CIA1_MASK and vector_restored):
            return
        unconfirmed_masks.remove(what)
        mask(what)
        late_mask_landed = True

    def restore_kernal_vector() -> None:
        nonlocal vector_restored
        landed = write_confirmed(
            api,
            lambda: api.write_regs(
                f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
            ),
        )
        if not landed and _vector_reads_back_kernal(api, log, who):
            landed = True
        if not landed:
            raise RuntimeError(
                f"the $0314 restore write was not confirmed after {CONFIRM_TRIES} tries"
            )
        vector_restored = True

    def drain_once() -> None:
        if drain is not None:
            drain()

    def drain_unmasked() -> None:
        if not (unconfirmed_masks and drain is not None and vector_restored):
            return
        log.error(
            "%s: the %s write was not confirmed, so the handler may have started a copy "
            "after the drain; waiting it out before releasing the bank",
            who,
            " and ".join(unconfirmed_masks),
        )
        drain()

    def drain_late_masked() -> None:
        if vector_restored or unconfirmed_masks or not (late_mask_landed and drain is not None):
            return
        log.error(
            "%s: $0314 is still hooked and a mask landed only on its retry; waiting out a "
            "copy the handler may have started before releasing the bank",
            who,
        )
        drain()

    def unmask_cia1() -> None:
        if not vector_restored:
            log.error(
                "%s: leaving CIA #1 Timer A masked — $0314 still points at the in-RAM "
                "handler, so re-arming the jiffy IRQ would vector through it",
                who,
            )
            return
        confirm(
            api,
            "CIA1 unmask",
            lambda: api.write_memory(f"{CIA1.ICR:04X}", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"),
        )

    def still_hooked() -> None:
        if vector_restored or if_still_hooked is None:
            return
        if_still_hooked()

    steps: list[tuple[str, Callable[[], object]]] = [
        (CIA1_MASK, lambda: mask(CIA1_MASK)),
        (RASTER_DISABLE, lambda: mask(RASTER_DISABLE)),
        ("drain", drain_once),
        ("kernal IRQ vector", restore_kernal_vector),
        ("drain unmasked handler", drain_unmasked),
        (f"{RASTER_DISABLE} retry", lambda: mask_again(RASTER_DISABLE)),
        (f"{CIA1_MASK} retry", lambda: mask_again(CIA1_MASK)),
        ("drain late-masked handler", drain_late_masked),
        ("raster flag ack", lambda: api.write_memory("D019", "01")),
        *before_unmask,
        ("CIA1 unmask", unmask_cia1),
        ("still hooked", still_hooked),
    ]
    run_teardown_steps(log, who, steps)
    return vector_restored


def release_leaked_raster_irq(
    api: C64Backend,
    log: logging.Logger,
    who: str,
    *,
    drain: Callable[[], None] | None = None,
) -> bool:
    """Defeat a raster handler a teardown may have left on `$0314`, and give
    the keyboard back: the unhook, with VIC bank 0 pinned between its ack and
    its CIA #1 unmask.

    A bank-swap teardown that failed before its unmask, or a `big_text` one
    whose restore never landed, leaves CIA #1 masked; with the raster source
    off nothing else runs SCNKEY, and the `$028D` key poller (pause, skip)
    stays dead until something unmasks it. The interstitial card runs this,
    and so does every playlist path that sets the next scene up straight
    after a teardown, with no card between them. Pinned after the restore and
    the ack, because a pin any earlier leaves a window in which a leaked
    handler re-flips `$DD00` to bank 2. Returns whether the restore landed."""
    return unhook_raster_irq(
        api,
        log,
        who,
        drain=drain,
        before_unmask=(
            (
                "VIC bank 0",
                lambda: api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}"),
            ),
        ),
    )
