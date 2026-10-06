"""ARMSID / ARM2SID: identify the chip, read and set its emulated model, and find
an ARM2SID's second (right) SID.

An ARMSID is an ARM emulation of a SID that sits in a physical socket, so unlike
a real 6581/8580 its model is a setting: autoconfig can switch it to whatever a
tune asks for instead of routing the tune to an UltiSID core. An ARM2SID is the
same chip with a second SID behind it. On a U64 its right channel answers at the
socket-1 base plus the offset the ``Ext DualSID Range Split`` address line
selects (``A5`` → ``$D420``), and its audio comes back through socket 2's mixer
channel — whatever socket 2's own address and enable items say.

Everything here speaks the chip's own register protocol (``sid_device_armsid.cc``
and ``U64Config::detectRemakes`` in the 1541ultimate firmware): writing ``SID``
to registers 29-31 enters a configuration mode, a two-letter command written to
31 then 30 leaves its two-byte reply in registers 27-28, and writing 0 to
register 29 leaves the mode. None of it makes a sound. The firmware reports an
ARM2SID as ``ARMSID`` because its test reads the channel letter from register
27, where this chip puts it in register 28, so the kind is decided here from the
chip's own reply rather than from ``SID Detected Socket N``.

Socket labels carry the result to the planners as ``"<kind> <model>"`` strings —
``"ARMSID 8580"``, ``"ARM2SID 6581"``, and ``"ARM2SID R 8580"`` for the right
channel standing in for socket 2 — next to the plain ``"6581"``/``"8580"`` a
real chip reports. :func:`socket_serves` and :func:`label_model` are how a
consumer compares them.

See docs/architecture/sid.md#armsidpy--armsid--arm2sid.
"""

from __future__ import annotations

import logging
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# `SID Detected Socket N` values that name this chip family.
DETECTED_TYPES: Final = frozenset({"ARMSID", "ARM2SID"})

KIND_ARMSID: Final = "ARMSID"
KIND_ARM2SID: Final = "ARM2SID"
_RIGHT_TAG: Final = "R"
MODELS: Final[tuple[str, str]] = ("6581", "8580")

# The firmware's per-socket category, registered only while an ARMSID is detected.
CAT_ARMSID_FMT: Final = "SID Socket {n}: ARMSID"
ITEM_ARMSID_MODE: Final = "Fundamental Mode"

# The address line an ARM2SID's right channel is decoded on, and where that puts
# it relative to the socket-1 base.
ITEM_EXT_SPLIT: Final = "Ext DualSID Range Split"
EXT_SPLIT_OFF: Final = "Off"
EXT_SPLIT_RIGHT: Final = "A5"
_EXT_SPLIT_OFFSET: Final[dict[str, int]] = {
    "A5": 0x20,
    "A6": 0x40,
    "A7": 0x80,
    "A8": 0x100,
    "A9": 0x200,
}
RIGHT_OFFSET: Final = _EXT_SPLIT_OFFSET[EXT_SPLIT_RIGHT]

# A c64cast-only (category, item) namespace for "set this socket's model", so a
# model change rides in the same plan dict, snapshot and restore as the REST
# items it travels with. `sid_hw_config._put_all` routes it to
# :func:`set_socket_model`; the firmware never sees this category name.
CAT_SOCKET_MODEL: Final = "c64cast: socket model"

_REG_REPLY: Final = 27
_REG_MODE: Final = 29
_REG_CMD_HI: Final = 30
_REG_CMD_LO: Final = 31
# The firmware waits 10 ms after a command before reading the reply; a DMA write
# and the REST read that follows travel separate paths, so allow twice that.
_SETTLE_S: Final = 0.02


def _write(api: C64Backend, address: int, value: int) -> None:
    api.write_memory(f"{address:04X}", f"{value:02X}")


def _enter(api: C64Backend, base: int) -> None:
    for offset, letter in ((_REG_MODE, "S"), (_REG_CMD_HI, "I"), (_REG_CMD_LO, "D")):
        _write(api, base + offset, ord(letter))
    time.sleep(_SETTLE_S)


def _leave(api: C64Backend, base: int) -> None:
    _write(api, base + _REG_MODE, 0)


def _reply(api: C64Backend, base: int) -> bytes | None:
    data = api.read_memory(base + _REG_REPLY, 2)
    return bytes(data) if data is not None and len(data) == 2 else None


def _query(api: C64Backend, base: int, command: str) -> bytes | None:
    _write(api, base + _REG_CMD_LO, ord(command))
    _write(api, base + _REG_CMD_HI, ord("I"))
    time.sleep(_SETTLE_S)
    return _reply(api, base)


@dataclass(frozen=True)
class ArmsidReply:
    """What the chip at one base says about itself. `channel` is ``"L"`` or
    ``"R"`` for an ARM2SID and None for an ARMSID; `model` is None when the
    model reply was not a 6581/8580."""

    channel: str | None
    model: str | None

    @property
    def kind(self) -> str:
        return KIND_ARM2SID if self.channel else KIND_ARMSID


def probe(api: C64Backend, base: int) -> ArmsidReply | None:
    """Ask the chip at `base` what it is, or None when no ARMSID answers there
    (an ordinary SID, an empty address, or a read the link could not make).
    Silent, and always leaves configuration mode."""
    try:
        _enter(api, base)
        if _reply(api, base) != b"NO":
            return None
        ident = _query(api, base, "I") or b""
        model_reply = _query(api, base, "F") or b""
    except Exception:  # noqa: BLE001 — best-effort, like every SID config read
        log.debug("armsid: probe at $%04X failed", base, exc_info=True)
        return None
    finally:
        try:
            _leave(api, base)
        except Exception:  # noqa: BLE001
            log.debug("armsid: leaving config mode at $%04X failed", base, exc_info=True)
    # The channel letter has been seen in register 28 (firmware 3.17); the U64
    # firmware's own test looks in register 27, so either is accepted.
    channel = next((chr(b) for b in ident if chr(b) in ("L", "R")), None)
    model = {ord("6"): "6581", ord("8"): "8580"}.get(model_reply[0]) if model_reply else None
    return ArmsidReply(channel=channel, model=model)


def write_model(api: C64Backend, base: int, model: str) -> None:
    """Switch the chip at `base` to `model` (``"6581"``/``"8580"``) through its
    register protocol. Takes effect at once and lasts until the chip is told
    otherwise or loses power; it is not saved to the chip's flash."""
    _enter(api, base)
    try:
        for offset, value in ((_REG_MODE, ord("S")), (_REG_CMD_HI, ord("E"))):
            _write(api, base + offset, value)
        _write(api, base + _REG_CMD_LO, ord(model[0]))
        time.sleep(_SETTLE_S)
    finally:
        _leave(api, base)


def label(kind: str, model: str | None, *, right: bool = False) -> str:
    """The socket label for a chip of this family."""
    parts = [kind, _RIGHT_TAG] if right else [kind]
    return " ".join([*parts, model or "?"])


def is_reconfigurable(socket_label: str | None) -> bool:
    """Whether the chip behind `socket_label` can be switched to either model.
    Only a label :func:`label` built counts: the firmware's bare ``"ARMSID"``
    is what a socket keeps when the chip did not answer its probe."""
    if socket_label is None:
        return False
    kind, _, rest = socket_label.partition(" ")
    return kind in DETECTED_TYPES and bool(rest)


def is_right_channel(socket_label: str | None) -> bool:
    """Whether `socket_label` is an ARM2SID's right channel standing in for
    socket 2 — realized through the Ext DualSID split, not socket 2's items."""
    return socket_label is not None and socket_label.startswith(f"{KIND_ARM2SID} {_RIGHT_TAG} ")


def label_model(socket_label: str | None) -> str | None:
    """The 6581/8580 model `socket_label` presents, or the label itself for a
    chip this module does not know (a real chip's label already is its model)."""
    if socket_label is None or not is_reconfigurable(socket_label):
        return socket_label
    model = socket_label.rsplit(" ", 1)[-1]
    return model if model in MODELS else None


def socket_serves(socket_label: str | None, required: str | None) -> bool:
    """Whether a socket carrying `socket_label` can play a chip that requires
    `required` — its own model, or any model once switched."""
    if socket_label is None:
        return False
    if required is None:
        return True
    if is_reconfigurable(socket_label):
        return required in MODELS
    return socket_label == required


def needs_model_change(socket_label: str | None, required: str | None) -> bool:
    """Whether claiming this socket for `required` means switching its model."""
    return (
        is_reconfigurable(socket_label)
        and required in MODELS
        and label_model(socket_label) != required
    )


def split_offset(split: str | None) -> int | None:
    """The address offset an `Ext DualSID Range Split` value puts the right
    channel at, or None when the split is off (the right channel answers
    nowhere)."""
    return _EXT_SPLIT_OFFSET.get(split or "")


# The last labels detected per backend, so a read-back that must not touch the
# bus mid-tune (the resolved-audio line, the source map) can still describe the
# chips. Weak, so a backend's labels go when it does.
_labels: weakref.WeakKeyDictionary[object, tuple[str | None, str | None]] = (
    weakref.WeakKeyDictionary()
)
# Where each backend's socket 1 was, and which split was live, at detection —
# what reaching the right channel later needs.
_socket1_base: weakref.WeakKeyDictionary[object, int] = weakref.WeakKeyDictionary()


def cached_labels(api: C64Backend) -> tuple[str | None, str | None] | None:
    """The labels :func:`detect_labels` last produced for `api`, if any."""
    try:
        return _labels.get(api)
    except TypeError:
        return None


def _remember(api: C64Backend, labels: tuple[str | None, str | None], base1: int | None) -> None:
    try:
        _labels[api] = labels
        if base1 is not None:
            _socket1_base[api] = base1
    except TypeError:  # an object that cannot be weakly referenced goes uncached
        pass


def _right_channel_split(split: str | None) -> tuple[int, str]:
    """Where the right channel answers relative to socket 1, and the split value
    that puts it there: the live split when it reaches the right channel at
    all, else ``A5``."""
    offset = split_offset(split)
    if offset is not None and split is not None:
        return offset, split
    return RIGHT_OFFSET, EXT_SPLIT_RIGHT


def _with_split(api: C64Backend, current: str | None, wanted: str, action: Callable[[], _T]) -> _T:
    """Run `action` with the Ext DualSID split at `wanted`, putting `current`
    back afterward when it differed."""
    from .asid_sidmap import CAT_ADDRESSING

    if current == wanted:
        return action()
    api.put_config_item(CAT_ADDRESSING, ITEM_EXT_SPLIT, wanted)
    try:
        return action()
    finally:
        if current is not None:
            api.put_config_item(CAT_ADDRESSING, ITEM_EXT_SPLIT, current)


def detect_labels(
    api: C64Backend,
    detected: tuple[str | None, str | None],
    bases: tuple[int | None, int | None],
    split: str | None,
) -> tuple[str | None, str | None]:
    """Socket labels for a machine whose firmware reports `detected` per socket
    (``None`` = empty), with each socket at `bases` (None = unmapped or
    disabled) and the Ext DualSID split at `split`.

    A socket the firmware reports as an ARMSID is probed and labeled by what it
    says. An ARM2SID in socket 1 with nothing detected in socket 2 has its right
    channel looked for at socket 1's base plus the split's offset — and when the
    split is off, under ``A5`` for the length of the probe, since that is the
    only way to reach it. A socket that is disabled, unmapped or silent keeps its firmware label,
    which no planner treats as reconfigurable."""
    labels: list[str | None] = list(detected)
    for index, (kind, base) in enumerate(zip(detected, bases, strict=True)):
        if kind not in DETECTED_TYPES or base is None:
            continue
        reply = probe(api, base)
        if reply is None:
            log.info(
                "armsid: socket %d reports %s but did not answer at $%04X — "
                "treating it as a fixed chip",
                index + 1,
                kind,
                base,
            )
            continue
        labels[index] = label(reply.kind, reply.model)

    base1 = bases[0]
    if (
        labels[0] is not None
        and labels[0].startswith(f"{KIND_ARM2SID} ")
        and detected[1] is None
        and base1 is not None
    ):
        offset, wanted = _right_channel_split(split)
        try:
            right = _with_split(api, split, wanted, lambda: probe(api, base1 + offset))
        except Exception:  # noqa: BLE001
            log.debug("armsid: right-channel probe failed", exc_info=True)
            right = None
        if right is not None and right.channel == _RIGHT_TAG:
            labels[1] = label(KIND_ARM2SID, right.model, right=True)

    result = (labels[0], labels[1])
    _remember(api, result, base1)
    return result


def set_socket_model(api: C64Backend, source: str, model: str) -> None:
    """Switch the ARMSID-family chip behind `source` (``"socket1"``/``"socket2"``)
    to `model`, per the labels :func:`detect_labels` last recorded.

    A chip in a physical socket is set through the firmware's own config item,
    which applies it at once and keeps the U64's menu truthful. An ARM2SID's
    right channel has no config item, so it is set through its registers —
    with the split moved to reach it when it is off, and put back after. A
    source with no ARMSID behind it is left alone."""
    from .asid_sidmap import CAT_ADDRESSING

    labels = cached_labels(api) or (None, None)
    index = {"socket1": 0, "socket2": 1}.get(source)
    if index is None or not is_reconfigurable(labels[index]):
        log.debug("armsid: no ARMSID behind %s — model %s not set", source, model)
        return
    if model not in MODELS:
        return
    socket_label = labels[index] or ""
    if is_right_channel(socket_label):
        base1 = _socket1_base.get(api)
        if base1 is None:
            return
        split = api.get_config_category(CAT_ADDRESSING).get(ITEM_EXT_SPLIT)
        offset, wanted = _right_channel_split(split)
        _with_split(api, split, wanted, lambda: write_model(api, base1 + offset, model))
    else:
        api.put_config_item(CAT_ARMSID_FMT.format(n=index + 1), ITEM_ARMSID_MODE, model)
    updated = list(labels)
    updated[index] = f"{socket_label.rsplit(' ', 1)[0]} {model}"
    _remember(api, (updated[0], updated[1]), None)
