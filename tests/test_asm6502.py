"""Offline tests for c64cast.hw.asm6502 — the two-pass 6502 assembler.

Every assertion is a hand-checked encoding. That is the point: the assembler is
what stands between readable 6502 source and a payload that hangs a real
machine, so its output is pinned to bytes rather than to its own behavior.
"""

from __future__ import annotations

import unittest

from c64cast.hw import asm6502


class AssembleTest(unittest.TestCase):
    def test_absolute_and_immediate_encodings(self):
        image = asm6502.assemble("LDA #$3E\nSTA $FF00\nRTS\n", 0x2000)
        self.assertEqual(image, bytes([0xA9, 0x3E, 0x8D, 0x00, 0xFF, 0x60]))

    def test_zero_page_literal_stays_two_bytes(self):
        self.assertEqual(asm6502.assemble("INC $FC\n", 0x2000), bytes([0xE6, 0xFC]))

    def test_hex_literal_is_not_mistaken_for_a_label(self):
        # "$D600" offers "D600" as a perfectly good identifier to a naive regex.
        self.assertEqual(asm6502.assemble("BIT $D600\n", 0x2000), bytes([0x2C, 0x00, 0xD6]))

    def test_backward_branch_displacement(self):
        image = asm6502.assemble("loop:   INX\n        BNE loop\n", 0x2000)
        self.assertEqual(image, bytes([0xE8, 0xD0, 0xFD]))

    def test_forward_branch_resolves_across_the_two_passes(self):
        image = asm6502.assemble(
            "        BEQ done\n        INX\n        INX\ndone:   RTS\n", 0x2000
        )
        self.assertEqual(image, bytes([0xF0, 0x02, 0xE8, 0xE8, 0x60]))

    def test_label_operand_is_sized_absolute_even_in_zero_page(self):
        # Pass 1 cannot know the address yet, so it must commit to 3 bytes and
        # pass 2 must agree — otherwise every later label shifts.
        image = asm6502.assemble("target: RTS\n        JMP target\n", 0x0080)
        self.assertEqual(image, bytes([0x60, 0x4C, 0x80, 0x00]))

    def test_indexed_label_operand(self):
        image = asm6502.assemble("        LDA table,X\ntable:  .byte $01\n", 0x2000)
        self.assertEqual(image, bytes([0xBD, 0x03, 0x20, 0x01]))

    def test_index_register_is_not_treated_as_a_label(self):
        self.assertEqual(asm6502.assemble("ASL A\n", 0x2000), bytes([0x0A]))

    def test_low_and_high_byte_of_a_label(self):
        image = asm6502.assemble(
            "        LDA #<msg\n        LDA #>msg\nmsg:    .byte $00\n", 0x2000
        )
        self.assertEqual(image, bytes([0xA9, 0x04, 0xA9, 0x20, 0x00]))

    def test_label_plus_offset(self):
        image = asm6502.assemble("        LDA blob+$0100,X\nblob:   RTS\n", 0x2000)
        self.assertEqual(image, bytes([0xBD, 0x03, 0x21, 0x60]))

    def test_comments_and_blank_lines_are_ignored(self):
        image = asm6502.assemble("\n; all comment\n   NOP  ; trailing\n\n", 0x2000)
        self.assertEqual(image, bytes([0xEA]))

    def test_labels_of_reports_addresses(self):
        labels = asm6502.labels_of("a:  NOP\nb:  JMP a\nc:  RTS\n", 0x1000)
        self.assertEqual(labels, {"a": 0x1000, "b": 0x1001, "c": 0x1004})


class DirectiveTest(unittest.TestCase):
    def test_byte_word_text_and_res(self):
        image = asm6502.assemble(
            '        .byte $01,$02\n        .word $1234\n        .text "CBM"\n        .res 3,$AA\n',
            0x2000,
        )
        self.assertEqual(image, bytes([0x01, 0x02, 0x34, 0x12, 0x43, 0x42, 0x4D]) + b"\xaa" * 3)

    def test_res_defaults_to_zero_fill(self):
        self.assertEqual(asm6502.assemble(".res 2\n", 0x2000), b"\x00\x00")

    def test_word_of_a_label_is_little_endian(self):
        self.assertEqual(asm6502.assemble("here:   .word here\n", 0x8123), bytes([0x23, 0x81]))


class ErrorTest(unittest.TestCase):
    def test_undefined_label_is_reported(self):
        with self.assertRaises(asm6502.AsmError):
            asm6502.assemble("JMP nowhere\n", 0x2000)

    def test_duplicate_label_is_reported(self):
        with self.assertRaisesRegex(asm6502.AsmError, "duplicate"):
            asm6502.assemble("a: NOP\na: NOP\n", 0x2000)

    def test_bad_mnemonic_names_its_line(self):
        with self.assertRaisesRegex(asm6502.AsmError, "line 2"):
            asm6502.assemble("NOP\nFROB $12\n", 0x2000)

    def test_bare_decimal_in_an_instruction_is_refused(self):
        # py65 encodes "LDX #31" as $31 = 49. Silently. This is the whole
        # reason the check exists: it selected a nonexistent VDC register.
        with self.assertRaisesRegex(asm6502.AsmError, "unprefixed number"):
            asm6502.assemble("LDX #31\n", 0x2000)

    def test_hex_prefixed_operands_are_accepted(self):
        self.assertEqual(asm6502.assemble("LDX #$1F\n", 0x2000), bytes([0xA2, 0x1F]))

    def test_directives_still_take_decimal(self):
        self.assertEqual(asm6502.assemble(".res 3\n", 0x2000), b"\x00\x00\x00")

    def test_label_with_a_digit_is_not_mistaken_for_a_number(self):
        image = asm6502.assemble("buf2:   RTS\n        JMP buf2\n", 0x2000)
        self.assertEqual(image, bytes([0x60, 0x4C, 0x00, 0x20]))

    def test_unknown_directive_is_reported(self):
        with self.assertRaisesRegex(asm6502.AsmError, "unknown directive"):
            asm6502.assemble(".quux 1\n", 0x2000)


if __name__ == "__main__":
    unittest.main()
