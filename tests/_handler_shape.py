"""Where a page-flip swap handler's branches go, for the tests that hold every
exit on the kernal chain."""

from __future__ import annotations

from py65.devices.mpu6502 import MPU
from py65.disassembler import Disassembler
from py65.memory import ObservableMemory

from c64cast.video.modes_irq import _ACCEPT_ANSWER, _PAGE_FLIP_CUE, BANK_SWAP_IRQ_HANDLER_ADDR


def exit_branches(code: bytes) -> tuple[list[int], int]:
    """The targets of `code`'s branches outside its palette cue, and the
    address of its chain to the kernal (the answer check it ends on)."""
    assert code.endswith(_ACCEPT_ANSWER), "the handler does not end on the answer check"
    cue_at = code.index(_PAGE_FLIP_CUE)
    cue = range(cue_at, cue_at + len(_PAGE_FLIP_CUE))
    mem = ObservableMemory()
    for i, b in enumerate(code):
        mem[BANK_SWAP_IRQ_HANDLER_ADDR + i] = b
    dis = Disassembler(MPU(memory=mem))
    chain = BANK_SWAP_IRQ_HANDLER_ADDR + len(code) - len(_ACCEPT_ANSWER)
    pc, targets = BANK_SWAP_IRQ_HANDLER_ADDR, []
    while pc < chain:
        length, text = dis.instruction_at(pc)
        is_branch = text.split()[0] in ("BEQ", "BNE", "BCS", "BCC", "BMI", "BPL")
        if is_branch and pc - BANK_SWAP_IRQ_HANDLER_ADDR not in cue:
            targets.append(int(text.split("$")[1], 16))
        pc += length
    assert pc == chain, "the instruction stream does not land on the chain"
    return targets, chain
