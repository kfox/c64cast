"""A two-pass 6502 assembler with labels, built on py65's per-line assembler.

c64cast's older 6502 payloads (the SID player in ``hw/api.py``) are hand-
assembled byte templates with named offsets. That is workable for thirty bytes
of straight-line code and unworkable for anything with branches: every inserted
instruction shifts every offset after it, and a wrong branch target is a hung
C64 rather than a failing assert. This module exists so the C128-mode resident
ROM (``hw/vdc_rom.py``) can be written as readable source with named labels.

py65 is already a hard dependency and its ``Assembler`` handles the part worth
not re-deriving — the opcode table and addressing-mode selection. What it does
not do is labels, so that is all this adds: pass 1 walks the source to fix every
label's address, pass 2 substitutes and emits.

Deliberately not a full assembler. There are no macros, no expressions beyond
``label±constant``, no relocation, and no object format — the output is a flat
``bytes`` at a fixed origin, which is what a DMA'd payload or a cartridge image
actually needs.

## The one sizing rule

Pass 1 has to know each instruction's length before it knows any label's value,
so a label operand is sized as **absolute** (3 bytes) even when the address
turns out to be in zero page. Write zero-page operands as literals (``LDA $FB``)
rather than labels and the two passes agree; a label that resolves into zero
page still assembles correctly, just as the 3-byte absolute form.
"""

from __future__ import annotations

import re
from typing import Final

from py65.assembler import Assembler as _Py65Assembler
from py65.devices.mpu6502 import MPU as _MPU

#: Branches are relative and always 2 bytes; pass 1 can size them without
#: knowing the target, and pass 2 needs the pc to compute the displacement.
_BRANCHES: Final = frozenset({"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"})

#: The only mnemonics with an accumulator addressing mode, so the only ones
#: whose lone ``A`` operand is syntax rather than a label.
_ACCUMULATOR_OPS: Final = frozenset({"ASL", "LSR", "ROL", "ROR"})

#: A number that starts a token without a ``$`` — see _reject_bare_decimal.
_BARE_DECIMAL = re.compile(r"(?<![$\w])\d")

_LABEL_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
# The leading lookbehind keeps hex literals out: without it ``$D600`` offers
# ``D600`` as a perfectly good identifier and the operand becomes a bad label.
_LABEL_REF = re.compile(r"(?<![$\w])([A-Za-z_][A-Za-z0-9_]*)\b((?:\s*[+-]\s*\$?[0-9A-Fa-f]+)?)")
_PLACEHOLDER: Final = 0xFFFF


class AsmError(ValueError):
    """A source line that could not be assembled, with its line number."""


def assemble(source: str, origin: int) -> bytes:
    """Assemble ``source`` to a flat image loaded at ``origin``.

    Supported per line: an optional ``label:``, then either one instruction in
    py65's syntax or one directive — ``.byte`` (comma-separated bytes),
    ``.word`` (little-endian 16-bit), ``.text`` (ASCII), or ``.res N[,fill]``
    (N filler bytes). ``;`` starts a comment. Label references may carry a
    ``+``/``-`` constant, and ``<label`` / ``>label`` take the low / high byte.
    """
    lines = _parse(source)
    labels = _size_pass(lines, origin)
    return _emit_pass(lines, labels, origin)


def labels_of(source: str, origin: int) -> dict[str, int]:
    """The address every label resolves to. Lets a caller (and its tests) refer
    to a routine or table inside an assembled image without recounting bytes."""
    return _size_pass(_parse(source), origin)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Line:
    __slots__ = ("lineno", "label", "stmt")

    def __init__(self, lineno: int, label: str | None, stmt: str) -> None:
        self.lineno = lineno
        self.label = label
        self.stmt = stmt


def _parse(source: str) -> list[_Line]:
    out: list[_Line] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        text = raw.split(";", 1)[0].strip()
        if not text:
            continue
        label = None
        m = _LABEL_DEF.match(text)
        if m:
            label = m.group(1)
            text = text[m.end() :].strip()
        if not text.startswith("."):
            _reject_bare_decimal(lineno, text)
        out.append(_Line(lineno, label, text))
    return out


def _reject_bare_decimal(lineno: int, stmt: str) -> None:
    """py65's assembler reads an unprefixed number as **hex**, silently: it
    encodes ``LDX #31`` and ``LDX #$31`` identically. Anyone writing register
    numbers reads that as decimal 31 and gets 49, which on a VDC selects a
    register that does not exist. Requiring the ``$`` makes the trap
    unreachable instead of merely documented.

    Directives are exempt — ``.byte``/``.word``/``.res`` go through this
    module's own parser, where an unprefixed number really is decimal."""
    if _BARE_DECIMAL.search(stmt):
        raise AsmError(
            f"line {lineno}: {stmt!r} has an unprefixed number. py65 would read "
            f"it as hex, not decimal — write it as $xx."
        )


def _size_pass(lines: list[_Line], origin: int) -> dict[str, int]:
    labels: dict[str, int] = {}
    pc = origin
    for line in lines:
        if line.label is not None:
            if line.label in labels:
                raise AsmError(f"line {line.lineno}: duplicate label {line.label!r}")
            labels[line.label] = pc
        if line.stmt:
            pc += _size_of(line)
    return labels


def _size_of(line: _Line) -> int:
    stmt = line.stmt
    if stmt.startswith("."):
        return len(_directive_bytes(line, {}))
    mnemonic = stmt.split(None, 1)[0].upper()
    if mnemonic in _BRANCHES:
        return 2
    # Every label is a 4-hex-digit placeholder here, so an operand that mentions
    # one sizes as absolute — see the module docstring's sizing rule.
    return len(_assemble_one(line, _substitute(stmt, {}, pass1=True), 0))


def _emit_pass(lines: list[_Line], labels: dict[str, int], origin: int) -> bytes:
    out = bytearray()
    pc = origin
    for line in lines:
        if not line.stmt:
            continue
        if line.stmt.startswith("."):
            chunk = _directive_bytes(line, labels)
        else:
            stmt = _substitute(line.stmt, labels, pass1=False)
            chunk = bytes(_assemble_one(line, stmt, pc))
        out += chunk
        pc += len(chunk)
    return bytes(out)


def _assemble_one(line: _Line, stmt: str, pc: int) -> list[int]:
    try:
        encoded = _ASM.assemble(stmt, pc)
    except Exception as e:  # py65 raises bare Exceptions with terse text
        raise AsmError(f"line {line.lineno}: cannot assemble {line.stmt!r}: {e}") from e
    if encoded is None:
        raise AsmError(f"line {line.lineno}: cannot assemble {line.stmt!r}")
    return encoded


def _substitute(stmt: str, labels: dict[str, int], *, pass1: bool) -> str:
    """Replace label references with ``$xxxx`` / ``$xx`` literals."""

    # `<label` / `>label` first: they are byte-wide and must not be widened.
    def byte_ref(m: re.Match[str]) -> str:
        value = _resolve(m.group(2), m.group(3), labels, pass1)
        return f"${(value & 0xFF) if m.group(1) == '<' else (value >> 8) & 0xFF:02X}"

    stmt = re.sub(r"([<>])" + _LABEL_REF.pattern, byte_ref, stmt)

    def word_ref(m: re.Match[str]) -> str:
        if _is_register_context(m):
            return m.group(0)
        return f"${_resolve(m.group(1), m.group(2), labels, pass1) & 0xFFFF:04X}"

    return _LABEL_REF.sub(word_ref, stmt)


def _is_register_context(m: re.Match[str]) -> bool:
    """Is this identifier part of the syntax rather than a label reference?

    Three things look exactly like a label to the regex: the mnemonic, the
    ``X``/``Y`` of an indexed operand, and the ``A`` of accumulator mode. All
    three are decided by position, not by spelling — excluding the bare letters
    everywhere would quietly break a label named ``a``."""
    if m.start() == 0:
        return True  # the mnemonic
    text = m.string
    if text[: m.start()].rstrip().endswith(","):
        return True  # the X / Y of an indexed operand
    return m.group(0).upper() == "A" and text.split(None, 1)[0].upper() in _ACCUMULATOR_OPS


def _resolve(name: str, offset_text: str, labels: dict[str, int], pass1: bool) -> int:
    if pass1:
        # The offset is deliberately not applied: ``label+$0100`` on the
        # placeholder wraps to $00FF, which py65 would size as zero-page and
        # pass 2 — with a real address — would size as absolute. Sizing has to
        # be decided by the placeholder alone.
        return _PLACEHOLDER
    if name in labels:
        base = labels[name]
    else:
        raise AsmError(f"undefined label {name!r}")
    if not offset_text:
        return base
    sign = -1 if offset_text.lstrip()[0] == "-" else 1
    digits = offset_text.lstrip()[1:].strip()
    value = int(digits[1:], 16) if digits.startswith("$") else int(digits, 10)
    return base + sign * value


def _directive_bytes(line: _Line, labels: dict[str, int]) -> bytes:
    head, _, rest = line.stmt.partition(" ")
    name = head.lower()
    rest = rest.strip()
    if name == ".byte":
        return bytes(_number(x, labels) & 0xFF for x in rest.split(","))
    if name == ".word":
        out = bytearray()
        for x in rest.split(","):
            value = _number(x, labels) & 0xFFFF
            out += bytes([value & 0xFF, value >> 8])
        return bytes(out)
    if name == ".text":
        return rest.strip('"').encode("ascii")
    if name == ".res":
        parts = rest.split(",")
        count = _number(parts[0], labels)
        fill = _number(parts[1], labels) & 0xFF if len(parts) > 1 else 0
        if count < 0:
            raise AsmError(f"line {line.lineno}: .res count is negative")
        return bytes([fill]) * count
    raise AsmError(f"line {line.lineno}: unknown directive {head!r}")


def _number(text: str, labels: dict[str, int]) -> int:
    text = text.strip()
    if not text:
        raise AsmError(f"empty value in directive: {text!r}")
    if text.startswith("$"):
        return int(text[1:], 16)
    if text.startswith(("<", ">")):
        inner = _number(text[1:], labels)
        return inner & 0xFF if text[0] == "<" else (inner >> 8) & 0xFF
    if text[0].isdigit() or text[0] == "-":
        return int(text, 10)
    m = _LABEL_REF.fullmatch(text)
    if m is None:
        raise AsmError(f"cannot parse value {text!r}")
    return _resolve(m.group(1), m.group(2), labels, pass1=not labels)


_ASM: Final = _Py65Assembler(_MPU())
