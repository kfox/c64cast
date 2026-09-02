"""Character ROM: resolver precedence, the structural verifier, the 6502 dump
stub, and the auto-install orchestration.

The stub tests actually *run* it on py65 (a hard dep — it's the WaveformScene's
host-side SID emulator), so the copy loop, the self-modified page pointers, the
bank save/restore and the completion flag are proven here rather than on
hardware. py65 has no banking, which is exactly right for this: a flat 64 K
memory with a charset sitting at $D000 is what the C64 looks like *after* the
stub clears CHAREN, so the test asserts the copy the stub is there to perform.
"""

# _FakeBackend below is a duck-typed C64Backend, not a subclass of one.
# pyright: reportArgumentType=false
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import quiet_logging
from py65.devices.mpu6502 import MPU

from c64cast.hw import char_rom
from c64cast.hw.api import (
    CHAR_ROM_DUMP_BYTES,
    CHAR_ROM_DUMP_DEST,
    CHAR_ROM_DUMP_STUB_ADDR,
    build_char_rom_dump_stub,
    char_rom_flag_addr,
)
from c64cast.hw.backend import BackendCapabilityError
from c64cast.hw.c64 import CPU, KERNAL, VECTORS

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _synth_charset(n_sets: int = 2) -> bytes:
    """A structurally valid charset: distinctive glyphs for screen codes
    $00-$7F, the reverse half built as their complement, screen code $20
    blank and $01 non-blank — exactly what `verify` insists on, without
    copying a byte of anyone's ROM."""
    out = bytearray()
    for s in range(n_sets):
        normal = bytearray()
        for code in range(0x80):
            if code == 0x20:
                normal += b"\x00" * 8  # space
            else:
                normal += bytes(((code * 8 + row + s) % 251) | 0x01 for row in range(8))
        out += normal
        out += bytes((~b) & 0xFF for b in normal)
    return bytes(out)


class _FakeProfile:
    def __init__(self):
        self.supports_read = True
        self.supports_run_prg = True


class _FakeBackend:
    """Minimal stand-in for a C64Backend. `dump_char_rom` is all `char_rom`
    itself needs; the lifecycle methods are here for the CLI command, which
    must reset and close the link whatever the dump did."""

    def __init__(self, data: bytes | None = None, *, error: Exception | None = None):
        self.data = data
        self.error = error
        self.calls = 0
        self.resets = 0
        self.closes = 0
        self.clear_loops = 0
        self.profile = _FakeProfile()

    def dump_char_rom(self, timeout: float = 10.0) -> bytes:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.data is not None
        return self.data

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closes += 1

    def run_basic_clear_loop(self, timeout: float = 5.0) -> None:
        self.clear_loops += 1


class _CharRomTestCase(unittest.TestCase):
    """Points the data root at a tmpdir and clears the process-wide glyph
    cache, so no test can see (or write to) the developer's real data dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()
        char_rom.invalidate_cache()
        self.addCleanup(char_rom.invalidate_cache)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._env.stop)

    def write_file(self, name: str, data: bytes) -> Path:
        p = Path(self._tmp.name) / name
        p.write_bytes(data)
        return p


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


class VerifyTest(unittest.TestCase):
    def test_accepts_a_synthesized_charset(self):
        for n_sets in (1, 2):
            with self.subTest(n_sets=n_sets):
                r = char_rom.verify(_synth_charset(n_sets))
                self.assertTrue(r.ok, r.error)
                self.assertIsNone(r.error)

    def test_rejects_short_input(self):
        r = char_rom.verify(b"\xaa" * 2047)
        self.assertFalse(r.ok)
        assert r.error is not None
        self.assertIn("too short", r.error)

    def test_rejects_io_or_ram_garbage(self):
        # The failure this exists to catch: the $01 bank never took, so the
        # copy read I/O registers / live RAM. Structurally that is *not* two
        # complementary halves, however plausible the bytes look.
        garbage = bytes((i * 37 + 11) & 0xFF for i in range(4096))
        r = char_rom.verify(garbage)
        self.assertFalse(r.ok)
        assert r.error is not None
        self.assertIn("complement", r.error)

    def test_rejects_all_zero_memory(self):
        self.assertFalse(char_rom.verify(bytes(2048)).ok)

    def test_rejects_blank_memory_that_passes_the_complement_check(self):
        # $00 × 1024 followed by $FF × 1024 *is* two complementary halves —
        # a dump that read empty RAM would look structurally perfect. The
        # "screen code $01 is not blank" check is the one that catches it.
        r = char_rom.verify(bytes(1024) + bytes([0xFF] * 1024))
        self.assertFalse(r.ok)
        assert r.error is not None
        self.assertIn("$01", r.error)

    def test_rejects_charset_whose_space_is_not_blank(self):
        data = bytearray(_synth_charset(1))
        data[0x20 * 8] = 0x18
        data[(0x20 + 0x80) * 8] = (~0x18) & 0xFF  # keep the halves complementary
        r = char_rom.verify(bytes(data))
        self.assertFalse(r.ok)
        assert r.error is not None
        self.assertIn("$20", r.error)

    def test_tolerates_the_stock_reverse_at_glyph_anomaly(self):
        # The stock 901225-01's reverse `@` ($80) is NOT the exact complement
        # of `@` ($00) — one byte differs. A verifier demanding exactness would
        # reject the very ROM it is meant to accept.
        data = bytearray(_synth_charset(1))
        data[(0x80) * 8 + 5] ^= 0x04
        self.assertTrue(char_rom.verify(bytes(data)).ok)

    def test_unknown_digest_is_a_note_not_a_failure(self):
        # A Swedish machine's charset is the one that user wants us to use.
        r = char_rom.verify(_synth_charset())
        self.assertTrue(r.ok)
        self.assertIn("unrecognized variant", r.note)
        self.assertIn("unrecognized variant", r.describe())

    def test_known_digest_is_reported(self):
        data = _synth_charset()
        with mock.patch.dict(
            char_rom.STOCK_DIGESTS, {char_rom.verify(data).sha256: "the stock one"}
        ):
            self.assertEqual(char_rom.verify(data).note, "the stock one")


# --------------------------------------------------------------------------
# resolve / load_glyphs
# --------------------------------------------------------------------------


class ResolveTest(_CharRomTestCase):
    def test_configured_path_wins(self):
        configured = self.write_file("mine.bin", _synth_charset())
        char_rom.install_data(_synth_charset(1))
        self.assertEqual(char_rom.resolve(str(configured)), configured)

    def test_data_dir_when_nothing_configured(self):
        char_rom.install_data(_synth_charset())
        self.assertEqual(char_rom.resolve(), char_rom.installed_path())

    def test_falls_through_to_legacy_checkout_path(self):
        legacy = self.write_file("legacy.bin", _synth_charset())
        with mock.patch.object(char_rom, "LEGACY_CHARGEN_PATH", str(legacy)):
            self.assertEqual(char_rom.resolve(), legacy)

    def test_none_when_nothing_resolves(self):
        with mock.patch.object(char_rom, "LEGACY_CHARGEN_PATH", "/nonexistent/chargen.bin"):
            self.assertIsNone(char_rom.resolve())

    def test_missing_configured_path_falls_through_rather_than_raising(self):
        char_rom.install_data(_synth_charset())
        self.assertEqual(char_rom.resolve("/nonexistent/x.bin"), char_rom.installed_path())

    def test_configured_path_expands_tilde(self):
        with mock.patch.object(char_rom.paths, "expand_user", return_value="/nope") as ex:
            char_rom.resolve("~/chargen.bin")
        ex.assert_called_once_with("~/chargen.bin")


class LoadGlyphsTest(_CharRomTestCase):
    def test_returns_the_first_2k_of_an_installed_rom(self):
        data = _synth_charset()
        char_rom.install_data(data)
        self.assertEqual(char_rom.load_glyphs(), data[:2048])

    def test_falls_back_to_builtin_when_nothing_resolves(self):
        from c64cast.video.framebuffer import _builtin_charset

        with mock.patch.object(char_rom, "LEGACY_CHARGEN_PATH", "/nonexistent/chargen.bin"):
            self.assertEqual(char_rom.load_glyphs(), _builtin_charset())

    def test_explicit_paths_are_not_served_from_the_shared_cache(self):
        # The cache is keyed by nothing (one shared charset is the point), so
        # an explicit path must bypass it or the second caller gets the first
        # caller's glyphs. Two independently-valid-but-distinct sets (not a
        # byte-reversed charset, which fails the complement check and falls
        # back to the builtin font instead of proving this).
        both_sets = _synth_charset(2)
        a = self.write_file("a.bin", both_sets[:2048])
        b = self.write_file("b.bin", both_sets[2048:])
        first = char_rom.load_glyphs(str(a))
        second = char_rom.load_glyphs(str(b))
        self.assertNotEqual(first, second)
        self.assertEqual(first, a.read_bytes()[:2048])

    def test_short_file_falls_back_to_builtin(self):
        from c64cast.video.framebuffer import _builtin_charset

        short = self.write_file("short.bin", b"\xff" * 100)
        with self.assertLogs("c64cast.hw.char_rom", level="WARNING"):
            glyphs = char_rom.load_glyphs(str(short))
        self.assertEqual(glyphs, _builtin_charset())

    def test_a_resolvable_but_garbage_file_falls_back_with_a_warning(self):
        # Two definitions of "a usable charset" used to live in this module:
        # install_data() ran verify(), but the load path only length-checked,
        # so any 2 KB file at a resolved path rendered garbage glyphs with no
        # diagnostic at all.
        from c64cast.video.framebuffer import _builtin_charset

        garbage = self.write_file("garbage.bin", bytes((i * 37 + 11) & 0xFF for i in range(2048)))
        with self.assertLogs("c64cast.hw.char_rom", level="WARNING") as logs:
            glyphs = char_rom.load_glyphs(str(garbage))
        self.assertEqual(glyphs, _builtin_charset())
        self.assertIn("does not look like a character ROM", "".join(logs.output))

    def test_missing_configured_path_warns_and_names_the_fallback(self):
        installed = char_rom.install_data(_synth_charset())
        with self.assertLogs("c64cast.hw.char_rom", level="WARNING") as logs:
            char_rom.load_glyphs("/nonexistent/mine.bin")
        message = "".join(logs.output)
        self.assertIn("/nonexistent/mine.bin", message)
        self.assertIn(str(installed), message)

    def test_invalidate_cache_re_resolves(self):
        from c64cast.video.framebuffer import _builtin_charset

        with mock.patch.object(char_rom, "LEGACY_CHARGEN_PATH", "/nonexistent/chargen.bin"):
            self.assertEqual(char_rom.load_glyphs(), _builtin_charset())
            data = _synth_charset()
            char_rom.install_data(data)  # installs *and* invalidates
            self.assertEqual(char_rom.load_glyphs(), data[:2048])


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------


class InstallTest(_CharRomTestCase):
    def test_round_trip(self):
        src = self.write_file("src.bin", _synth_charset())
        dest = char_rom.install(str(src))
        self.assertEqual(dest, char_rom.installed_path())
        self.assertEqual(dest.read_bytes(), src.read_bytes())

    def test_refuses_to_write_something_that_is_not_a_charset(self):
        src = self.write_file("junk.bin", bytes((i * 37) & 0xFF for i in range(4096)))
        with self.assertRaises(ValueError):
            char_rom.install(str(src))
        self.assertFalse(char_rom.installed_path().exists())

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            char_rom.install("/nonexistent/chargen.bin")


# --------------------------------------------------------------------------
# The 6502 dump stub
# --------------------------------------------------------------------------


class DumpStubTest(unittest.TestCase):
    def test_banks_the_character_rom_in_and_restores_the_caller_bank(self):
        stub = build_char_rom_dump_stub(irq_exit=False)
        self.assertEqual(stub[0], 0x78, "must SEI: the KERNAL IRQ can't ack CIA #1 with I/O out")
        # LDA $01 / PHA … LDA #$33 / STA $01
        self.assertEqual(stub[1:4], bytes([0xA5, CPU.PORT, 0x48]))
        self.assertEqual(stub[4:8], bytes([0xA9, CPU.PORT_CHARROM, 0x85, CPU.PORT]))
        # PLA / STA $01 restores whatever the caller had, rather than assuming $37.
        self.assertIn(bytes([0x68, 0x85, CPU.PORT]), stub)

    def test_sys_tail_reenables_interrupts_and_returns(self):
        stub = build_char_rom_dump_stub(irq_exit=False)
        self.assertEqual(stub[-3:-1], bytes([0x58, 0x60]), "CLI then RTS")

    def test_irq_tail_restores_the_vector_and_chains_to_the_kernal(self):
        stub = build_char_rom_dump_stub(irq_exit=True)
        tail = stub[-14:-1]
        self.assertEqual(tail[:2], bytes([0xA9, KERNAL.IRQ_HANDLER & 0xFF]))
        self.assertEqual(tail[2:5], bytes([0x8D, VECTORS.IRQ & 0xFF, VECTORS.IRQ >> 8]))
        self.assertEqual(
            tail[-3:], bytes([0x4C, KERNAL.IRQ_HANDLER & 0xFF, KERNAL.IRQ_HANDLER >> 8])
        )
        self.assertNotIn(0x58, tail, "no CLI inside an IRQ handler — RTI restores the I flag")

    def test_stub_does_not_overlap_the_landing_zone(self):
        for irq_exit in (False, True):
            stub = build_char_rom_dump_stub(irq_exit=irq_exit)
            end = CHAR_ROM_DUMP_STUB_ADDR + len(stub)
            self.assertLess(
                end,
                CHAR_ROM_DUMP_DEST,
                "the copy would overwrite the stub mid-flight",
            )

    def test_flag_is_the_last_byte_and_starts_clear(self):
        stub = build_char_rom_dump_stub(irq_exit=False)
        self.assertEqual(stub[-1], 0x00)
        self.assertEqual(char_rom_flag_addr(stub), CHAR_ROM_DUMP_STUB_ADDR + len(stub) - 1)

    def test_page_pointers_are_patched_relative_to_the_base(self):
        base = 0x7000
        stub = build_char_rom_dump_stub(base=base, irq_exit=False)
        # The two INC operands must address this build's own LDA/STA operand
        # bytes, or a relocation silently bumps the wrong memory.
        src_operand = base + 14
        dst_operand = base + 17
        self.assertIn(bytes([0xEE, src_operand & 0xFF, src_operand >> 8]), stub)
        self.assertIn(bytes([0xEE, dst_operand & 0xFF, dst_operand >> 8]), stub)

    def _run(self, stub: bytes, *, base: int, irq_exit: bool) -> bytearray:
        """Execute the stub on py65 over a flat 64 K with a charset at $D000
        (i.e. the memory map the stub itself creates by clearing CHAREN)."""
        mem = bytearray(0x10000)
        mem[base : base + len(stub)] = stub
        mem[0xD000 : 0xD000 + CHAR_ROM_DUMP_BYTES] = _synth_charset()
        mem[CPU.PORT] = CPU.PORT_DEFAULT
        mem[VECTORS.IRQ] = base & 0xFF
        mem[VECTORS.IRQ + 1] = base >> 8
        mpu = MPU(memory=mem)
        mpu.pc = base
        if not irq_exit:
            # A return address for RTS to land on, so the run has a clear end.
            mem[0x01FF], mem[0x01FE] = 0x99, 0x98
            mpu.sp = 0xFD
        done = KERNAL.IRQ_HANDLER if irq_exit else 0x9999
        for _ in range(200_000):
            if mpu.pc == done:
                return mem
            mpu.step()
        self.fail(f"stub did not reach ${done:04X} — it hangs")

    def test_copies_the_whole_rom_and_raises_the_flag(self):
        for irq_exit in (False, True):
            with self.subTest(irq_exit=irq_exit):
                stub = build_char_rom_dump_stub(irq_exit=irq_exit)
                mem = self._run(stub, base=CHAR_ROM_DUMP_STUB_ADDR, irq_exit=irq_exit)
                copied = bytes(mem[CHAR_ROM_DUMP_DEST : CHAR_ROM_DUMP_DEST + CHAR_ROM_DUMP_BYTES])
                self.assertEqual(copied, _synth_charset())
                self.assertTrue(char_rom.verify(copied).ok)
                self.assertEqual(mem[char_rom_flag_addr(stub)], 0xFF)

    def test_leaves_the_cpu_port_as_it_found_it(self):
        # A botched $01 restore is the failure mode that wedges the machine.
        stub = build_char_rom_dump_stub(irq_exit=False)
        mem = self._run(stub, base=CHAR_ROM_DUMP_STUB_ADDR, irq_exit=False)
        self.assertEqual(mem[CPU.PORT], CPU.PORT_DEFAULT)

    def test_irq_variant_restores_the_kernal_vector(self):
        stub = build_char_rom_dump_stub(irq_exit=True)
        mem = self._run(stub, base=CHAR_ROM_DUMP_STUB_ADDR, irq_exit=True)
        self.assertEqual(mem[VECTORS.IRQ], KERNAL.IRQ_HANDLER & 0xFF)
        self.assertEqual(mem[VECTORS.IRQ + 1], KERNAL.IRQ_HANDLER >> 8)

    def test_runs_correctly_when_relocated(self):
        base = 0x7000
        stub = build_char_rom_dump_stub(base=base, irq_exit=False)
        mem = self._run(stub, base=base, irq_exit=False)
        copied = bytes(mem[CHAR_ROM_DUMP_DEST : CHAR_ROM_DUMP_DEST + CHAR_ROM_DUMP_BYTES])
        self.assertEqual(copied, _synth_charset())
        self.assertEqual(mem[char_rom_flag_addr(stub, base)], 0xFF)


# --------------------------------------------------------------------------
# dump / ensure_installed orchestration
# --------------------------------------------------------------------------


class DumpTest(_CharRomTestCase):
    def test_dump_returns_verified_bytes(self):
        data = _synth_charset()
        self.assertEqual(char_rom.dump(_FakeBackend(data)), data)

    def test_dump_rejects_garbage_from_the_backend(self):
        garbage = bytes((i * 37 + 11) & 0xFF for i in range(4096))
        with self.assertRaises(RuntimeError):
            char_rom.dump(_FakeBackend(garbage))


class EnsureInstalledTest(_CharRomTestCase):
    def _cfg(self, *, enabled: bool = True, charset_path: str | None = None):
        from c64cast.app import config as cfgmod

        cfg = cfgmod.Config()
        cfg.hardware.dump_char_rom = enabled
        cfg.preview.charset_path = charset_path
        return cfg

    def setUp(self):
        super().setUp()
        # Every case here is about "nothing is installed yet", so keep the
        # developer's own checkout ROM out of the resolver.
        p = mock.patch.object(char_rom, "LEGACY_CHARGEN_PATH", "/nonexistent/chargen.bin")
        p.start()
        self.addCleanup(p.stop)

    def test_dumps_and_installs_on_the_first_run(self):
        be = _FakeBackend(_synth_charset())
        with self.assertLogs("c64cast.hw.char_rom", level="INFO"):
            self.assertTrue(char_rom.ensure_installed(be, self._cfg()))
        self.assertEqual(char_rom.installed_path().read_bytes(), _synth_charset())

    def test_primed_fallback_is_dropped_so_this_run_benefits(self):
        from c64cast.video.framebuffer import _builtin_charset

        self.assertEqual(char_rom.load_glyphs(), _builtin_charset())  # primes the cache
        char_rom.ensure_installed(_FakeBackend(_synth_charset()), self._cfg())
        self.assertEqual(char_rom.load_glyphs(), _synth_charset()[:2048])

    def test_second_run_does_not_re_dump(self):
        be = _FakeBackend(_synth_charset())
        char_rom.ensure_installed(be, self._cfg())
        self.assertFalse(char_rom.ensure_installed(be, self._cfg()))
        self.assertEqual(be.calls, 1)

    def test_skipped_when_a_charset_is_already_configured(self):
        configured = self.write_file("mine.bin", _synth_charset())
        be = _FakeBackend(_synth_charset())
        self.assertFalse(char_rom.ensure_installed(be, self._cfg(charset_path=str(configured))))
        self.assertEqual(be.calls, 0)

    def test_a_garbage_configured_file_does_not_suppress_the_dump(self):
        # A resolved-but-unverifiable file used to count as "already have a
        # charset", permanently skipping the auto-dump behind glyphs that
        # never rendered right in the first place.
        configured = self.write_file("mine.bin", bytes((i * 37 + 11) & 0xFF for i in range(2048)))
        be = _FakeBackend(_synth_charset())
        self.assertTrue(char_rom.ensure_installed(be, self._cfg(charset_path=str(configured))))
        self.assertEqual(be.calls, 1)

    def test_escape_hatch_disables_it(self):
        be = _FakeBackend(_synth_charset())
        self.assertFalse(char_rom.ensure_installed(be, self._cfg(enabled=False)))
        self.assertEqual(be.calls, 0)

    def test_skipped_on_a_backend_that_cannot_read(self):
        be = _FakeBackend(_synth_charset())
        be.profile.supports_read = False
        self.assertFalse(char_rom.ensure_installed(be, self._cfg()))
        self.assertEqual(be.calls, 0)

    def test_skipped_on_a_backend_without_run_prg(self):
        be = _FakeBackend(_synth_charset())
        be.profile.supports_run_prg = False
        self.assertFalse(char_rom.ensure_installed(be, self._cfg()))
        self.assertEqual(be.calls, 0)

    def test_a_failing_dump_writes_nothing_and_never_raises(self):
        for err in (
            RuntimeError("no reply"),
            BackendCapabilityError("dump_char_rom"),
            OSError("link died"),
        ):
            with self.subTest(err=type(err).__name__):
                with self.assertLogs("c64cast.hw.char_rom", level="WARNING") as logs:
                    self.assertFalse(
                        char_rom.ensure_installed(_FakeBackend(error=err), self._cfg())
                    )
                self.assertFalse(char_rom.installed_path().exists())
                self.assertIn("--install-char-rom", "".join(logs.output))

    def test_garbage_dump_writes_nothing_and_leaves_the_fallback(self):
        from c64cast.video.framebuffer import _builtin_charset

        garbage = bytes((i * 37 + 11) & 0xFF for i in range(4096))
        with self.assertLogs("c64cast.hw.char_rom", level="WARNING"):
            self.assertFalse(char_rom.ensure_installed(_FakeBackend(garbage), self._cfg()))
        self.assertFalse(char_rom.installed_path().exists())
        self.assertEqual(char_rom.load_glyphs(), _builtin_charset())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class InstallCharRomCliTest(_CharRomTestCase):
    """--install-char-rom through the real argparse entry point."""

    def _main(self, argv: list[str]) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        from c64cast.app.cli import main

        buf = io.StringIO()
        with quiet_logging(), redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_installs_and_reports_where_it_landed(self):
        src = self.write_file("src.bin", _synth_charset())
        rc, out = self._main(["--install-char-rom", str(src)])
        self.assertEqual(rc, 0)
        self.assertIn(str(char_rom.installed_path()), out)
        self.assertEqual(char_rom.installed_path().read_bytes(), src.read_bytes())

    def test_unreadable_file_is_a_usage_error(self):
        rc, _ = self._main(["--install-char-rom", "/nonexistent/chargen.bin"])
        self.assertEqual(rc, 2)

    def test_file_that_is_not_a_charset_is_a_usage_error(self):
        src = self.write_file("junk.bin", bytes((i * 37) & 0xFF for i in range(4096)))
        rc, _ = self._main(["--install-char-rom", str(src)])
        self.assertEqual(rc, 2)
        self.assertFalse(char_rom.installed_path().exists())

    def test_needs_no_hardware_and_no_config(self):
        # It must dispatch before anything resolves a config or opens a link —
        # this runs in a tmp cwd with no c64cast.toml and no machine present.
        src = self.write_file("src.bin", _synth_charset())
        with mock.patch(
            "c64cast.hw.backend.make_backend", side_effect=AssertionError("no hardware")
        ):
            self.assertEqual(self._main(["--install-char-rom", str(src)])[0], 0)


class DumpCharRomCliTest(_CharRomTestCase):
    """--dump-char-rom through the real entry point, against a fake backend."""

    def _run(self, be) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        from c64cast.app.cli import main

        buf = io.StringIO()
        with mock.patch("c64cast.app.cli_commands.make_backend", return_value=be):
            with quiet_logging(), redirect_stdout(buf):
                rc = main(["--dump-char-rom", "-u", "u64://198.51.100.1"])
        return rc, buf.getvalue()

    def test_dumps_installs_and_resets(self):
        be = _FakeBackend(_synth_charset())
        with mock.patch("time.sleep"):
            rc, out = self._run(be)
        self.assertEqual(rc, 0)
        self.assertEqual(char_rom.installed_path().read_bytes(), _synth_charset())
        self.assertIn(str(char_rom.installed_path()), out)
        self.assertEqual(be.closes, 1)
        self.assertGreaterEqual(be.resets, 1, "must leave the machine reset")

    def test_re_dumps_over_an_existing_file(self):
        char_rom.install_data(_synth_charset(1))
        fresh = _synth_charset(2)
        be = _FakeBackend(fresh)
        with mock.patch("time.sleep"):
            self.assertEqual(self._run(be)[0], 0)
        self.assertEqual(be.calls, 1, "the flag re-dumps unconditionally")
        self.assertEqual(char_rom.installed_path().read_bytes(), fresh)

    def test_capability_error_exits_3(self):
        be = _FakeBackend(error=BackendCapabilityError("dump_char_rom"))
        with mock.patch("time.sleep"):
            self.assertEqual(self._run(be)[0], 3)
        self.assertFalse(char_rom.installed_path().exists())

    def test_dump_failure_exits_4_and_still_closes_the_link(self):
        be = _FakeBackend(error=RuntimeError("stub never signaled"))
        with mock.patch("time.sleep"):
            self.assertEqual(self._run(be)[0], 4)
        self.assertEqual(be.closes, 1)
        self.assertFalse(char_rom.installed_path().exists())


if __name__ == "__main__":
    unittest.main()
