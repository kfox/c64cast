"""Host-side client for the Ultimate Command Interface (UCI).

The UCI is the four-register window at $DF1C-$DF1F that a C64 program uses to
ask the Ultimate for things. c64cast reaches it without running 6502 code: the
Ultimate decodes a DMA read and a DMA write on the cartridge bus through
whichever bank configuration is in force, so its I/O registers answer the same
read/write path the rest of c64cast already uses.

The handshake drives the same registers and control bits the firmware's own
6502 clients do (see `software/6502/unsorted/uci_wedge.s` in
GideonZ/1541ultimate): wait for the control register to read idle, push the
target and command bytes into $DF1D, raise PUSH_CMD, wait for BUSY to clear,
then drain $DF1E while the control register reports data and $DF1F while it
reports status, and finally accept or abort.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

UCI_CONTROL = 0xDF1C
UCI_COMMAND = 0xDF1D
UCI_RESULT = 0xDF1E
UCI_STATUS = 0xDF1F

CONTROL_PUSH_CMD = 0x01
CONTROL_NEXT_DATA = 0x02
CONTROL_ABORT = 0x04
CONTROL_CLEAR_ERROR = 0x08

# Reading $DF1C returns `slot_status` from command_protocol.vhd: bit 7
# response_valid, bit 6 status_valid, bits 5-4 state, bit 3 error_busy, bits 2-0
# handshake_in. A handshake bit stays set after the write that raised it until
# the Ultimate has processed it, so the state bits alone do not say the
# interface is free.
STATE_MASK = 0x30
STATE_IDLE = 0x00
STATE_BUSY = 0x10

DATA_AVAILABLE = 0x80
STATUS_AVAILABLE = 0x40
ERROR_BUSY = 0x08
HANDSHAKE_MASK = 0x07

# The control target answers "NN,TEXT", and 00 is its only success code
# (`c_status_ok`). The separator is checked too: no repeated byte spells it, so
# RAM cannot pass for the interface.
STATUS_OK = b"00,"

TARGET_CONTROL = 0x04
CMD_GET_PALETTE = 0x51

PALETTE_COLORS = 16
PALETTE_BYTES = PALETTE_COLORS * 3

_POLL_S = 0.005
_IDLE_TIMEOUT_S = 1.0
_BUSY_TIMEOUT_S = 2.0
# CMD_MAX_STATUS_LEN, from the firmware's io/command_interface/command_intf.h.
_STATUS_LIMIT = 256


class MemoryBus(Protocol):
    """The part of a backend a UCI transaction needs."""

    def read_memory(self, address: int, length: int, timeout: float = ...) -> bytes | None: ...

    def write_memory(self, address: str, data_hex: str) -> None: ...

    def flush(self) -> None: ...


def _read_control(bus: MemoryBus) -> int | None:
    data = bus.read_memory(UCI_CONTROL, 1)
    return data[0] if data else None


def _poll_control(bus: MemoryBus, accept: Callable[[int], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        control = _read_control(bus)
        if control is None:
            return False
        if accept(control):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def _is_settled(control: int) -> bool:
    return control & STATE_MASK == STATE_IDLE and not control & HANDSHAKE_MASK


def _await_idle(bus: MemoryBus) -> bool:
    return _poll_control(bus, _is_settled, _IDLE_TIMEOUT_S)


def _await_not_busy(bus: MemoryBus) -> bool:
    return _poll_control(bus, lambda c: c & STATE_MASK != STATE_BUSY, _BUSY_TIMEOUT_S)


def _write_byte(bus: MemoryBus, address: int, value: int) -> None:
    bus.write_memory(f"{address:04X}", f"{value & 0xFF:02X}")


def _write_control(bus: MemoryBus, value: int) -> None:
    _write_byte(bus, UCI_CONTROL, value)
    bus.flush()


def _push_command(bus: MemoryBus, target: int, command: int) -> None:
    # $DF1D is a FIFO port, so each byte needs its own single-byte write — a
    # two-byte block write would land the second byte on $DF1E instead.
    _write_byte(bus, UCI_COMMAND, target)
    _write_byte(bus, UCI_COMMAND, command)
    _write_control(bus, CONTROL_PUSH_CMD)


def _drain(bus: MemoryBus, port: int, ready_bit: int, limit: int) -> bytes:
    out = bytearray()
    for _ in range(limit):
        control = _read_control(bus)
        if control is None or not control & ready_bit:
            break
        data = bus.read_memory(port, 1)
        if not data:
            break
        out += data
    return bytes(out)


def _clear_stale_error(bus: MemoryBus) -> None:
    """Drop an error_busy flag left by an earlier client's rejected push.

    The Ultimate raises it when a command arrives while the interface is not
    idle and never clears it itself, so it outlives whatever set it.
    """
    control = _read_control(bus)
    if control is not None and control & ERROR_BUSY:
        log.debug("UCI: clearing a stale error_busy flag")
        _write_control(bus, CONTROL_CLEAR_ERROR)


def _took_command(bus: MemoryBus) -> bool:
    """Whether the push moved the interface off idle, as a real one must.

    `command_protocol.vhd` sets the state to BUSY on the clock edge of the
    PUSH_CMD write, so a control register still reading idle on the next bus
    cycle is not the Command Interface: with I/O banked out these four
    addresses are RAM cells, which read back the byte just written to them.
    """
    control = _read_control(bus)
    return control is not None and control & STATE_MASK != STATE_IDLE


def _finish(bus: MemoryBus, accepted: bool, *, confirm: bool) -> None:
    """Hand the interface back, and confirm it actually went.

    Two things have to be true before the interface is free again: the state
    bits back to idle, and any handshake bit still pending cleared by the
    Ultimate. Neither is implied by having written the byte — on a U64 the DMA
    socket sits idle through ~110 REST reads and can be closed under the
    transaction, and a lost ack leaves the interface in a data state where it
    blocks every later command, the firmware's own clients included. Checking
    catches a dropped release whatever the cause, and ABORT is honored from any
    state.

    `confirm` is False when nothing took the command, where insisting on a
    settled register spends two timeouts and warns about a machine that was
    never asked anything. The release is still written: "not the interface" is
    read off one register, and a wedge is the expensive way to be wrong.
    """
    _write_control(bus, CONTROL_NEXT_DATA if accepted else CONTROL_ABORT)
    if not confirm or _await_idle(bus):
        return

    _write_control(bus, CONTROL_ABORT)
    if not _await_idle(bus):
        log.warning(
            "UCI: the Ultimate's Command Interface did not return to idle, and "
            "may stay busy for its other clients until the machine is reset"
        )


def _transact(
    bus: MemoryBus, target: int, command: int, reply_bytes: int
) -> tuple[bytes, bytes] | None:
    """Run one command to completion and return its (reply, status).

    None means the interface never got as far as answering. Whatever happens
    after the command is pushed, `_finish` hands the interface back: accepted
    when the whole reply arrived, aborted otherwise. CONTROL_NEXT_DATA is
    ignored by the hardware unless the state machine is in a data state, so a
    command abandoned while it is still BUSY has to be aborted instead.
    """
    if not _await_idle(bus):
        log.debug("UCI: control register never reported idle")
        return None
    _clear_stale_error(bus)

    _push_command(bus, target, command)
    took_command = _took_command(bus)
    answer = None
    try:
        if not _await_not_busy(bus):
            log.debug("UCI: command $%02X stayed busy", command)
        else:
            answer = (
                _drain(bus, UCI_RESULT, DATA_AVAILABLE, reply_bytes),
                _drain(bus, UCI_STATUS, STATUS_AVAILABLE, _STATUS_LIMIT),
            )
    finally:
        _finish(
            bus,
            answer is not None and len(answer[0]) == reply_bytes,
            confirm=took_command,
        )
    return answer


def _as_rgb_triples(payload: bytes) -> tuple[tuple[int, int, int], ...]:
    return tuple((payload[i], payload[i + 1], payload[i + 2]) for i in range(0, len(payload), 3))


def read_palette_rgb(bus: MemoryBus) -> tuple[tuple[int, int, int], ...] | None:
    """The 16 colors the Ultimate is currently driving, as RGB triples.

    This is the machine's *active* palette, so it reflects a .vpl loaded from
    flash — which the REST config API names but will not serve.

    Returns None whenever the machine does not answer the command: firmware
    without it replies "21,UNKNOWN COMMAND", registers that are not the
    interface never leave idle when the command is pushed, and a backend that
    cannot read memory raises. Every failure is a debug log and a None; the
    caller picks the fallback.
    """
    try:
        answer = _transact(bus, TARGET_CONTROL, CMD_GET_PALETTE, PALETTE_BYTES)
    except Exception:
        log.debug("UCI: get-palette failed", exc_info=True)
        return None
    if answer is None:
        return None

    payload, status = answer
    if not status.startswith(STATUS_OK):
        log.debug("UCI: get-palette answered %r", bytes(status[:40]))
        return None
    if len(payload) != PALETTE_BYTES:
        log.debug("UCI: palette reply was %d bytes, wanted %d", len(payload), PALETTE_BYTES)
        return None
    return _as_rgb_triples(payload)
