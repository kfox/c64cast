"""c64cast.hw.uci — the host-side Ultimate Command Interface client."""

from __future__ import annotations

import unittest
from unittest import mock

from c64cast.hw import uci

PALETTE_RGB = tuple((i * 3, i * 3 + 1, i * 3 + 2) for i in range(uci.PALETTE_COLORS))
PALETTE_BYTES = bytes(v for color in PALETTE_RGB for v in color)
STATUS_OK = b"00,OK"
STATUS_UNKNOWN_COMMAND = b"21,UNKNOWN COMMAND"
# The state the firmware reports alongside the last data byte, and the only data
# state a single-part reply such as the palette reaches. Its bit is `state(1)`,
# which is what gates NEXT_DATA and the data and status bits alike.
_STATE_LAST_DATA = 0x20

# Well above the ~110 reads a palette costs and the ~615 a maximal status drain
# costs. Every fake stops answering here so a drain that has lost its bound
# fails the run instead of hanging it, which reports nothing.
_READ_CEILING = 1000


class _CountedBus:
    """Per-instance read counter, so no fake can loop forever."""

    _reads = 0

    def _count_read(self) -> None:
        self._reads += 1
        if self._reads > _READ_CEILING:
            raise AssertionError(f"unbounded read loop: more than {_READ_CEILING} reads")


class _FakeUltimate(_CountedBus):
    """An Ultimate that answers the UCI handshake at $DF1C-$DF1F.

    Models the register semantics the real interface has: $DF1D is a
    write-only FIFO, $DF1E and $DF1F pop one byte per read, the control
    register at $DF1C reports the state plus whether data or status is waiting,
    and — as `command_protocol.vhd` gates it — a NEXT_DATA write is ignored
    unless the state machine has reached a data state, while an ABORT write is
    honored from anywhere.
    """

    def __init__(
        self,
        *,
        reply: bytes = PALETTE_BYTES,
        status: bytes = STATUS_OK,
        busy_before_push: int = 0,
        busy_after_push: int = 1,
        read_fails_after: int | None = None,
        stuck_busy: bool = False,
        stuck_status: bool = False,
        lose_ack: bool = False,
        handshake_reads: int = 4,
    ) -> None:
        self.reply = reply
        self.status = status
        self.busy_before_push = busy_before_push
        self.busy_after_push = busy_after_push
        self.read_fails_after = read_fails_after
        self.stuck_busy = stuck_busy
        self.stuck_status = stuck_status
        self.lose_ack = lose_ack
        self.handshake_reads = handshake_reads

        self.commands: list[int] = []
        self.control_writes: list[int] = []
        self.flushes = 0
        self.state = uci.STATE_IDLE
        self.handshake = 0
        self.error_busy = False
        self._handshake_seen = 0
        self._pending = b""
        self._status_out = b""
        self._reads = 0
        self._pushed = False

    @property
    def released(self) -> bool:
        return self._pushed and self.state == uci.STATE_IDLE

    def _control(self) -> int:
        if not self._pushed and self.busy_before_push > 0:
            self.busy_before_push -= 1
            return uci.STATE_BUSY

        if self._pushed and self.state == uci.STATE_BUSY and not self.stuck_busy:
            if self.busy_after_push > 0:
                self.busy_after_push -= 1
            else:
                self.state = _STATE_LAST_DATA

        # `response_valid` and `status_valid` are both `state(1) and not
        # handshake_in(2)` in command_protocol.vhd, so nothing is readable until
        # the state machine reaches a data state. An abort here goes straight
        # back to idle, so the state bit alone carries both terms.
        readable = bool(self.state & _STATE_LAST_DATA)
        value = self.state | self.handshake
        if self._pending and readable:
            value |= uci.DATA_AVAILABLE
        if (self._status_out or self.stuck_status) and readable:
            value |= uci.STATUS_AVAILABLE
        if self.error_busy:
            value |= uci.ERROR_BUSY
        # The Ultimate takes time to process a handshake bit. The count is
        # deliberately more than the reads a client makes on its way to the
        # next push, so only one that actually polls for the bit gets through.
        if self.handshake:
            stuck = self.lose_ack and self.handshake == uci.CONTROL_NEXT_DATA
            self._handshake_seen += 1
            if self._handshake_seen > self.handshake_reads and not stuck:
                self.handshake = 0
                self._handshake_seen = 0
        return value

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        self._count_read()
        if self.read_fails_after is not None and self._reads > self.read_fails_after:
            return None
        if address == uci.UCI_CONTROL:
            return bytes([self._control()])
        if address == uci.UCI_RESULT:
            head, self._pending = self._pending[:1], self._pending[1:]
            return head
        if address == uci.UCI_STATUS:
            if self.stuck_status:
                return b"!"
            head, self._status_out = self._status_out[:1], self._status_out[1:]
            return head
        raise AssertionError(f"unexpected read at ${address:04X}")

    def write_memory(self, address: str, data_hex: str) -> None:
        addr = int(address, 16)
        value = int(data_hex, 16)
        if addr == uci.UCI_COMMAND:
            self.commands.append(value)
            return
        if addr == uci.UCI_CONTROL:
            self.control_writes.append(value)
            self.handshake |= value & uci.HANDSHAKE_MASK
            if value == uci.CONTROL_CLEAR_ERROR:
                self.error_busy = False
                return
            if value == uci.CONTROL_PUSH_CMD:
                early = self.state != uci.STATE_IDLE or self.handshake & ~value
                if early:
                    self.error_busy = True
                self._pushed = True
                self.state = uci.STATE_BUSY
                self._pending = b"" if early else self.reply
                self._status_out = b"" if early else self.status
            elif value == uci.CONTROL_NEXT_DATA:
                if self.state & _STATE_LAST_DATA and not self.lose_ack:
                    self.state = uci.STATE_IDLE
            elif value == uci.CONTROL_ABORT:
                self.stuck_busy = False
                self.lose_ack = False
                self.state = uci.STATE_IDLE
            return
        raise AssertionError(f"unexpected write at ${addr:04X}")

    def flush(self) -> None:
        self.flushes += 1


class _ConstantBus(_CountedBus):
    """A bus whose every read answers the same byte, and whose writes vanish."""

    def __init__(self, value: int) -> None:
        self.value = value

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        self._count_read()
        return bytes([self.value])

    def write_memory(self, address: str, data_hex: str) -> None:
        pass

    def flush(self) -> None:
        pass


class _RamBus(_CountedBus):
    """What `$DF1C-$DF1F` answer when they are not the interface at all.

    A DMA read lands wherever the C64's current bank configuration points it,
    so these are four unrelated cells: one steady byte each, nothing pops on a
    read, and a write changes nothing — the shape that keeps a control cell
    claiming idle-with-data for as long as it is asked.
    """

    def __init__(self, control: int, result: int, status: int) -> None:
        self.cells = {
            uci.UCI_CONTROL: control,
            uci.UCI_RESULT: result,
            uci.UCI_STATUS: status,
        }

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        self._count_read()
        return bytes([self.cells[address]])

    def write_memory(self, address: str, data_hex: str) -> None:
        pass

    def flush(self) -> None:
        pass


class _WriteThroughRamBus(_CountedBus):
    """Four RAM cells that read back what was written to them.

    The other half of "not the interface": with I/O banked out a DMA write
    lands in RAM, so the control cell answers with the PUSH_CMD byte rather
    than with a state, and the release write it is handed back reads as a
    handshake bit no Ultimate is ever going to clear.
    """

    def __init__(self) -> None:
        self.cells = {
            uci.UCI_CONTROL: 0x00,
            uci.UCI_COMMAND: 0x00,
            uci.UCI_RESULT: 0x00,
            uci.UCI_STATUS: 0x00,
        }
        self.control_writes: list[int] = []

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        self._count_read()
        return bytes([self.cells[address]])

    def write_memory(self, address: str, data_hex: str) -> None:
        addr, value = int(address, 16), int(data_hex, 16)
        self.cells[addr] = value
        if addr == uci.UCI_CONTROL:
            self.control_writes.append(value)

    def flush(self) -> None:
        pass


class _DataPortDiesMidDrain(_FakeUltimate):
    """An Ultimate whose read link drops inside the drain: the control register
    goes on promising data, and the result port answers nothing."""

    _DIES_AFTER = 8
    _result_reads = 0

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        if address == uci.UCI_RESULT:
            self._result_reads += 1
            if self._result_reads > self._DIES_AFTER:
                self._count_read()
                return None
        return super().read_memory(address, length, timeout)


class _ControlPortDiesMidDrain(_FakeUltimate):
    """An Ultimate whose read link drops on the register that paces the drain:
    the control register stops answering once eight bytes are out, while the
    result and status ports still would. Both drains end there regardless,
    because each reads the control register before every byte."""

    _DIES_AFTER = 8

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        drained = len(self.reply) - len(self._pending)
        if address == uci.UCI_CONTROL and self._pushed and drained >= self._DIES_AFTER:
            self._count_read()
            return None
        return super().read_memory(address, length, timeout)


class _RaisingBus:
    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        raise RuntimeError("this backend cannot read memory")

    def write_memory(self, address: str, data_hex: str) -> None:
        pass

    def flush(self) -> None:
        pass


class ReadPaletteTest(unittest.TestCase):
    def test_returns_the_sixteen_colors(self):
        self.assertEqual(uci.read_palette_rgb(_FakeUltimate()), PALETTE_RGB)

    def test_sends_the_control_target_and_get_palette_command(self):
        device = _FakeUltimate()
        uci.read_palette_rgb(device)
        # ControlTarget id 4, CTRL_CMD_GET_PALETTE 0x51 — firmware wire values,
        # so asserting them against our own constants would prove nothing.
        self.assertEqual(device.commands, [0x04, 0x51])

    def test_accepts_the_reply_and_leaves_the_interface_idle(self):
        device = _FakeUltimate()
        uci.read_palette_rgb(device)
        self.assertEqual(device.control_writes, [0x01, 0x02])
        self.assertTrue(device.released)

    def test_uses_the_firmwares_register_addresses(self):
        self.assertEqual(
            (uci.UCI_CONTROL, uci.UCI_COMMAND, uci.UCI_RESULT, uci.UCI_STATUS),
            (0xDF1C, 0xDF1D, 0xDF1E, 0xDF1F),
        )

    def test_reads_the_control_register_the_way_the_vhdl_lays_it_out(self):
        # slot_status in command_protocol.vhd. The fakes are built from these
        # same constants, so nothing else here would notice one moving.
        self.assertEqual(
            (
                uci.DATA_AVAILABLE,
                uci.STATUS_AVAILABLE,
                uci.STATE_MASK,
                uci.STATE_IDLE,
                uci.STATE_BUSY,
                uci.ERROR_BUSY,
                uci.HANDSHAKE_MASK,
            ),
            (0x80, 0x40, 0x30, 0x00, 0x10, 0x08, 0x07),
        )

    def test_writes_the_control_bits_the_firmware_acts_on(self):
        self.assertEqual(
            (
                uci.CONTROL_PUSH_CMD,
                uci.CONTROL_NEXT_DATA,
                uci.CONTROL_ABORT,
                uci.CONTROL_CLEAR_ERROR,
            ),
            (0x01, 0x02, 0x04, 0x08),
        )

    def test_waits_out_a_busy_interface(self):
        self.assertEqual(uci.read_palette_rgb(_FakeUltimate(busy_after_push=5)), PALETTE_RGB)

    def test_nothing_is_readable_until_the_command_leaves_busy(self):
        """Draining before BUSY clears reads nothing at all: neither port
        hands its reply to a client that asks early."""
        device = _FakeUltimate(busy_after_push=5)
        with mock.patch.object(uci, "_await_not_busy", return_value=True):
            self.assertIsNone(uci.read_palette_rgb(device))
        self.assertEqual(device._pending, PALETTE_BYTES)
        self.assertEqual(device._status_out, STATUS_OK)

    def test_waits_for_a_non_idle_interface_to_settle(self):
        self.assertEqual(uci.read_palette_rgb(_FakeUltimate(busy_before_push=3)), PALETTE_RGB)

    def test_a_second_transaction_succeeds_back_to_back(self):
        """The state bits going idle do not mean the interface is free: a
        handshake bit stays set until the Ultimate has processed it, and the
        HANDSHAKE_RESET that clears it rewinds the command pointer, so a
        command pushed before then is read as a zero-length one and answered
        empty. Observed on hardware as a first read that works and a second
        that never does."""
        device = _FakeUltimate()
        self.assertEqual(uci.read_palette_rgb(device), PALETTE_RGB)
        device.reply, device.status = PALETTE_BYTES, STATUS_OK
        device.busy_after_push = 1
        self.assertEqual(uci.read_palette_rgb(device), PALETTE_RGB)
        self.assertFalse(device.error_busy)

    def test_clears_a_stale_error_flag_before_pushing(self):
        device = _FakeUltimate()
        device.error_busy = True
        self.assertEqual(uci.read_palette_rgb(device), PALETTE_RGB)
        self.assertIn(uci.CONTROL_CLEAR_ERROR, device.control_writes)
        self.assertFalse(device.error_busy)

    def test_confirms_the_interface_went_idle(self):
        device = _FakeUltimate()
        uci.read_palette_rgb(device)
        self.assertEqual(device.state, uci.STATE_IDLE)

    def test_drains_the_status_reply(self):
        device = _FakeUltimate()
        uci.read_palette_rgb(device)
        self.assertEqual(device._status_out, b"")


class ReadPaletteFailureTest(unittest.TestCase):
    """Every failure answers None so the caller can fall back, and none of them
    leaves the interface mid-transaction."""

    def setUp(self):
        for name in ("_IDLE_TIMEOUT_S", "_BUSY_TIMEOUT_S"):
            patcher = mock.patch.object(uci, name, 0.02)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_none_when_the_interface_never_goes_idle(self):
        device = _FakeUltimate(busy_before_push=10_000, handshake_reads=0)
        self.assertIsNone(uci.read_palette_rgb(device))

    def test_no_command_is_pushed_when_the_interface_never_goes_idle(self):
        device = _FakeUltimate(busy_before_push=10_000, handshake_reads=0)
        uci.read_palette_rgb(device)
        self.assertEqual(device.commands, [])

    def test_none_when_the_command_stays_busy(self):
        self.assertIsNone(uci.read_palette_rgb(_FakeUltimate(stuck_busy=True, handshake_reads=0)))

    def test_a_stuck_command_is_aborted_not_acked(self):
        device = _FakeUltimate(stuck_busy=True, handshake_reads=0)
        uci.read_palette_rgb(device)
        # The hardware ignores NEXT_DATA outside a data state, so only an abort
        # actually hands a wedged interface back.
        self.assertEqual(device.control_writes, [0x01, 0x04])
        self.assertTrue(device.released)

    def test_none_when_the_reply_is_short(self):
        self.assertIsNone(
            uci.read_palette_rgb(_FakeUltimate(reply=PALETTE_BYTES[:24], handshake_reads=0))
        )

    def test_a_short_reply_is_aborted_not_acked(self):
        device = _FakeUltimate(reply=PALETTE_BYTES[:24], handshake_reads=0)
        uci.read_palette_rgb(device)
        self.assertEqual(device.control_writes, [0x01, 0x04])

    def test_none_when_the_firmware_does_not_know_the_command(self):
        device = _FakeUltimate(reply=b"", status=STATUS_UNKNOWN_COMMAND, handshake_reads=0)
        self.assertIsNone(uci.read_palette_rgb(device))

    def test_none_when_the_status_reports_an_error_for_a_full_length_reply(self):
        device = _FakeUltimate(status=b"21,UNKNOWN COMMAND", handshake_reads=0)
        self.assertIsNone(uci.read_palette_rgb(device))

    def test_none_when_nothing_answers_but_the_bits_look_ready(self):
        # A bus reading back a constant DATA_AVAILABLE would otherwise pass the
        # length check with 48 identical bytes and install them process-wide.
        for value in (0x80, 0xC0, 0xA0, 0xE0):
            with self.subTest(value=value):
                self.assertIsNone(uci.read_palette_rgb(_ConstantBus(value)))

    def test_none_when_the_registers_are_really_ram(self):
        # Every read a steady byte, but a *different* one per address, so the
        # control cell can claim idle-with-data while the status cell spells
        # ASCII "0" 256 times. Checking only the two digits of the status code
        # would accept that as "00" and install 48 bytes of $41 as a palette.
        device = _RamBus(control=0xC0, result=0x41, status=0x30)
        self.assertIsNone(uci.read_palette_rgb(device))

    def test_ram_that_reads_back_its_writes_is_not_taken_for_a_wedge(self):
        # A push moves a real interface off idle on the next clock, so a
        # control register still reading idle is not one. Escalating there
        # spends both idle timeouts and warns that a machine which was never
        # asked anything may need a reset.
        device = _WriteThroughRamBus()
        with self.assertNoLogs("c64cast.hw.uci", level="WARNING"):
            self.assertIsNone(uci.read_palette_rgb(device))
        self.assertEqual(device.control_writes.count(uci.CONTROL_ABORT), 1)

    def test_a_lost_ack_is_escalated_to_an_abort(self):
        # The ack is the last write of a transaction whose read phase can
        # outlive the write link, so a release that is written but never lands
        # would otherwise leave the interface wedged for every later command.
        device = _FakeUltimate(lose_ack=True, handshake_reads=0)
        self.assertEqual(uci.read_palette_rgb(device), PALETTE_RGB)
        self.assertEqual(device.control_writes, [0x01, 0x02, 0x04])
        self.assertTrue(device.released)

    def test_none_when_the_machine_stops_answering_reads(self):
        with self.assertLogs("c64cast.hw.uci", level="WARNING") as logs:
            self.assertIsNone(uci.read_palette_rgb(_FakeUltimate(read_fails_after=4)))
        # A machine that stops answering cannot be handed back either, and
        # leaving its Command Interface busy is worth saying out loud.
        self.assertIn("did not return to idle", "".join(logs.output))

    def test_a_data_port_that_stops_answering_mid_drain_is_a_short_reply(self):
        # The link can drop inside the ~110 reads a palette costs, while the
        # control register goes on reporting data. The status here is the real
        # "00,OK", so only the length check stands between a truncated reply and
        # 8 bytes installed process-wide as 16 colors. The log is asserted
        # because a drain that read on regardless would raise instead, and this
        # transaction would still end in None and an abort.
        device = _DataPortDiesMidDrain(handshake_reads=0)
        with self.assertLogs("c64cast.hw.uci", level="DEBUG") as logs:
            self.assertIsNone(uci.read_palette_rgb(device))
        self.assertIn("palette reply was 8 bytes", "".join(logs.output))
        # Stopping there spares the rest of the 48 reads on a link already gone.
        self.assertEqual(device._result_reads, device._DIES_AFTER + 1)
        self.assertEqual(device.control_writes, [0x01, 0x04])
        self.assertTrue(device.released)

    def test_a_control_port_that_stops_answering_mid_drain_loses_the_status_too(self):
        # The same dropped link one register over: a drain spends a control
        # read per byte, so that is the read as likely to go. It paces the
        # status drain as well, so this ends at the status check rather than at
        # the length check — a drain that took the miss for a byte would raise
        # on `None & int` instead, and the log is what tells the two apart.
        device = _ControlPortDiesMidDrain(handshake_reads=0)
        with self.assertLogs("c64cast.hw.uci", level="DEBUG") as logs:
            self.assertIsNone(uci.read_palette_rgb(device))
        self.assertIn("get-palette answered", "".join(logs.output))
        self.assertEqual(device._pending, PALETTE_BYTES[device._DIES_AFTER :])
        # The abort cannot be confirmed either — the register that would say so
        # is the one that died — so the release escalates to a second abort.
        self.assertEqual(device.control_writes, [0x01, 0x04, 0x04])
        self.assertIn("did not return to idle", "".join(logs.output))

    def test_none_when_the_backend_cannot_read_memory(self):
        self.assertIsNone(uci.read_palette_rgb(_RaisingBus()))

    def test_an_endless_status_reply_is_bounded(self):
        device = _FakeUltimate(stuck_status=True, handshake_reads=0)
        uci.read_palette_rgb(device)
        # A ceiling of the test's own, not a multiple of _STATUS_LIMIT:
        # bounding the assertion by the constant under test passes for any
        # value of it.
        self.assertLess(device._reads, _READ_CEILING)


if __name__ == "__main__":
    unittest.main()
