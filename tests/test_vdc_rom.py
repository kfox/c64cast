"""Offline tests for c64cast.hw.vdc_rom — the C128-mode cartridge ROM.

The ROM is executed, not just assembled: ``_fakes.VdcMachine`` runs the real
cartridge image on a py65 6502 with the ``$D600``/``$D601`` porthole wired to a
``FakeVdc``, so boot, the VDC register program, the screen clear, and the blit
command are all checked end to end against the resulting video RAM.

This is where the ROM gets debugged. The bench has no RGBI capture, so a fault
that reaches hardware costs a human squinting at a CRT to characterize; a fault
that reaches here costs a failing assert naming the byte.
"""

from __future__ import annotations

import unittest

from _fakes import VdcMachine

from c64cast.hw import vdc, vdc_rom


def booted() -> VdcMachine:
    """A machine that has run the cartridge through to its idle loop."""
    machine = VdcMachine(vdc_rom.build_rom(), load=vdc_rom.CART_ADDR)
    machine.run_to(vdc_rom.resident_labels()["poll"])
    return machine


class ImageTest(unittest.TestCase):
    def test_autostart_header_matches_the_c128_signature(self):
        rom = vdc_rom.build_rom()
        entry = vdc_rom.CART_ADDR + 10
        self.assertEqual(rom[:10], vdc.c128_autostart_signature(entry))

    def test_rom_is_one_8k_bank(self):
        self.assertEqual(len(vdc_rom.build_rom()), 0x2000)

    def test_resident_image_is_exactly_what_the_stub_copies(self):
        self.assertEqual(len(vdc_rom.build_resident()), vdc_rom.RESIDENT_MAX)

    def test_resident_leaves_headroom(self):
        # The stub copies two fixed pages; a loop that outgrows them would be
        # truncated on the machine and merely truncated-looking here.
        used = len(vdc_rom.assemble(vdc_rom.resident_source(), vdc_rom.RESIDENT_ADDR))
        self.assertLess(used, vdc_rom.RESIDENT_MAX)

    def test_register_table_is_generated_from_the_shared_program(self):
        # The cartridge and the host-driven path must not drift apart.
        source = vdc_rom.resident_source()
        for reg, value in vdc.BITMAP_640x200_REGS.items():
            self.assertIn(f".byte ${reg:02X},${value:02X}", source)

    def test_crt_container_declares_a_c128_cartridge(self):
        crt = vdc_rom.build_crt()
        self.assertEqual(crt[:14], b"C128 CARTRIDGE")
        # The firmware routes to rtBinC128 only on this exact combination.
        self.assertEqual(crt[0x18], 0)  # EXROM
        self.assertEqual(crt[0x19], 0)  # GAME
        self.assertEqual(crt[0x4C:0x4E], b"\x00\x00")  # CHIP load address
        self.assertEqual(crt[0x4E:0x50], b"\x20\x00")  # CHIP ROM size


class BootTest(unittest.TestCase):
    def test_reaches_the_idle_loop(self):
        booted()  # run_to raises if it never gets there

    def test_banks_itself_into_all_ram_with_io(self):
        self.assertEqual(booted().memory.ram[0xFF00], vdc_rom.MMU_ALL_RAM_IO)

    def test_blanks_the_vic_screen(self):
        # The 40-col screen is off because the VDC is the display — and because
        # a blanked VIC-II is what makes 2 MHz safe during a blit.
        self.assertEqual(booted().memory.ram[0xD011] & 0x10, 0)

    def test_points_the_nmi_and_irq_vectors_at_an_rti(self):
        machine = booted()
        rti = vdc_rom.resident_labels()["isr"]
        ram = machine.memory.ram
        self.assertEqual(ram[0xFFFA] | (ram[0xFFFB] << 8), rti)
        self.assertEqual(ram[0xFFFE] | (ram[0xFFFF] << 8), rti)
        self.assertEqual(ram[rti], 0x40)  # RTI

    def test_programs_the_bitmap_register_set(self):
        regs = booted().vdc.regs
        # R18/R19 and the block-op registers are working state by the time the
        # screen clear has run; everything else is the program as written.
        working = {
            vdc.R.UPDATE_HI,
            vdc.R.UPDATE_LO,
            vdc.R.V_SCROLL_CTRL,
            vdc.R.WORD_COUNT,
            vdc.R.DATA,
        }
        for reg, value in vdc.BITMAP_640x200_REGS.items():
            if reg not in working:
                self.assertEqual(regs[reg], value, f"R{reg}")

    def test_clears_the_bitmap_and_flat_fills_the_attributes(self):
        ram = booted().vdc.ram
        self.assertEqual(set(ram[vdc.BITMAP_BASE : vdc.BITMAP_BASE + vdc.BITMAP_BYTES]), {0x00})
        self.assertEqual(
            set(ram[vdc.ATTR_BASE : vdc.ATTR_BASE + vdc.ATTR_BYTES]),
            {vdc_rom.ATTR_INIT},
        )

    def test_clear_does_not_run_past_the_attribute_area(self):
        # An off-by-one in vfill's 16-bit chunk arithmetic would show up as
        # spill into the bytes just above, which nothing else would notice.
        ram = booted().vdc.ram
        end = vdc.ATTR_BASE + vdc.ATTR_BYTES
        self.assertEqual(set(ram[end : end + 256]), {0x00})

    def test_heartbeat_advances_while_idle(self):
        machine = booted()
        before = machine.memory.ram[vdc_rom.MAIL_BEAT]
        machine.steps(200)
        self.assertNotEqual(machine.memory.ram[vdc_rom.MAIL_BEAT], before)

    def test_idle_loop_leaves_the_porthole_alone(self):
        # The host drives the VDC directly between commands, so the idle loop
        # must not be touching it.
        machine = booted()
        machine.vdc.regs[vdc.R.CURSOR_HI] = 0x5A
        machine.steps(500)
        self.assertEqual(machine.vdc.regs[vdc.R.CURSOR_HI], 0x5A)


class CommandTest(unittest.TestCase):
    def _run_command(self, machine: VdcMachine, cmd: int, **fields: int) -> None:
        """Stage the parameters, then commit with the command byte — the same
        two-step the host does, for the same reason."""
        ram = machine.memory.ram
        done = ram[vdc_rom.MAIL_DONE]
        for addr, value in fields.items():
            ram[getattr(vdc_rom, addr)] = value
        ram[vdc_rom.MAIL_CMD] = cmd
        machine.run_until_byte(vdc_rom.MAIL_DONE, (done + 1) & 0xFF)
        self.assertEqual(ram[vdc_rom.MAIL_CMD], 0, "command was not acknowledged")

    def _blit(self, machine: VdcMachine, payload: bytes, dest: int) -> None:
        src = vdc_rom.FRAMEBUF_ADDR
        machine.memory.ram[src : src + len(payload)] = payload
        self._run_command(
            machine,
            vdc_rom.CMD_BLIT,
            MAIL_SRC_LO=src & 0xFF,
            MAIL_SRC_HI=src >> 8,
            MAIL_DST_LO=dest & 0xFF,
            MAIL_DST_HI=dest >> 8,
            MAIL_CNT_LO=len(payload) & 0xFF,
            MAIL_CNT_HI=len(payload) >> 8,
        )

    def test_blit_lands_the_exact_bytes_at_the_exact_address(self):
        machine = booted()
        payload = bytes(range(256)) * 2
        self._blit(machine, payload, 0x0500)
        self.assertEqual(machine.vdc.ram[0x0500 : 0x0500 + len(payload)], payload)

    def test_blit_spanning_a_page_boundary_does_not_repeat_a_page(self):
        # The page loop advances the source high byte by hand; getting that
        # wrong writes page 0 twice and is invisible in a uniform payload.
        machine = booted()
        payload = bytes((i * 7 + 3) & 0xFF for i in range(513))
        self._blit(machine, payload, 0x1000)
        self.assertEqual(machine.vdc.ram[0x1000 : 0x1000 + 513], payload)

    def test_blit_of_a_sub_page_count_stops_at_the_count(self):
        machine = booted()
        guard = machine.vdc.ram[0x0200 + 100]
        self._blit(machine, b"\xa5" * 100, 0x0200)
        self.assertEqual(machine.vdc.ram[0x0200:0x0264], b"\xa5" * 100)
        self.assertEqual(machine.vdc.ram[0x0200 + 100], guard)

    def test_blit_leaves_the_cpu_back_at_1mhz(self):
        # The loop must not idle at 2 MHz: TeensyROM+ DMA against a 2 MHz C128
        # is unverified, and a host that cannot be heard cannot be recovered.
        machine = booted()
        self._blit(machine, b"\x11" * 32, 0x0000)
        self.assertEqual(machine.memory.ram[0xD030] & 0x01, 0)

    def test_vdc_reg_command_writes_one_register(self):
        machine = booted()
        self._run_command(
            machine,
            vdc_rom.CMD_VDC_REG,
            MAIL_ARG=vdc.R.DISPLAY_HI,
            MAIL_DST_LO=0x3E,
        )
        self.assertEqual(machine.vdc.regs[vdc.R.DISPLAY_HI], 0x3E)

    def test_unknown_command_is_acknowledged_rather_than_hanging(self):
        machine = booted()
        self._run_command(machine, 0x7F)

    def test_consecutive_commands_each_advance_the_done_counter(self):
        machine = booted()
        self._blit(machine, b"\x01" * 16, 0x0000)
        self._blit(machine, b"\x02" * 16, 0x0040)
        self.assertEqual(machine.memory.ram[vdc_rom.MAIL_DONE], 2)


if __name__ == "__main__":
    unittest.main()
