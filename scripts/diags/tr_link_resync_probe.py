#!/usr/bin/env python3
"""Does a TeensyROM+ command that fails part way leave the link out of step?

    scripts/diags/tr_link_resync_probe.py --url tr://
    scripts/diags/tr_link_resync_probe.py --url tr:// --case short --case cut-send

Each case drives the real firmware into one of the states `TRClient` has to
recover from, then checks the next command and the C64 RAM it touched:

  * ``short``     — a WriteC64Mem frame whose payload stops part way, sent raw,
    then nothing. Reports when the firmware gives up and what it says, and
    whether the short frame wrote anything.
  * ``swallow``   — the same short frame followed at once by a whole
    WriteC64Mem, sent raw: the firmware reads the second frame as the first
    one's payload (c64cast#540). Reports both targets.
  * ``cut-send``  — `TRClient.write_segment` with a send that raises OSError
    after part of the frame, then a second write_segment. Reports whether the
    second write landed, whether the first target was corrupted, and how long
    the second command took.
  * ``cut-idle``  — ``cut-send`` with two idle seconds before the second
    write, longer than the resync's wait, so that write waits only the short
    quiet.
  * ``late-ack``  — a 4 KiB write_segment under a 1 ms io_timeout, so its ack
    arrives after the client has given up (c64cast#640), then two reads.
  * ``late-read`` — a 4 KiB read_segment under a 1 ms io_timeout, so its data
    arrives after the client has given up, then a read of the KERNAL reset
    vector.

Writes land at $4000-$40FF, $5000-$50FF and $6000-$6FFF (plain RAM). Resets
the machine on exit.
"""

from __future__ import annotations

import argparse
import contextlib
import time
from collections.abc import Iterator

import _diaglib as d

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend
from c64cast.hw.teensyrom_api import TeensyROMBackend
from c64cast.hw.teensyrom_dma import (
    TOK_WRITE_C64_MEM,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
    TRTransport,
)

A = 0x4000
B = 0x5000
BULK = 0x6000
KERNAL_RESET_VECTOR = 0xFFFC


def _frame(addr: int, length: int, payload: bytes) -> bytes:
    return TRClient._u16(TOK_WRITE_C64_MEM) + TRClient._u16(addr) + TRClient._u16(length) + payload


@contextlib.contextmanager
def _read_timeout(tx: TRTransport, seconds: float) -> Iterator[None]:
    """Shorten both the client's deadline and the OS read's own wait."""
    prev = tx.io_timeout  # type: ignore[attr-defined]
    tx.io_timeout = seconds  # type: ignore[attr-defined]
    if isinstance(tx, SerialTransport):
        ser = tx._ser
        prev_os = ser.timeout  # type: ignore[union-attr]
        ser.timeout = seconds  # type: ignore[union-attr]
    elif isinstance(tx, TcpTransport):
        sock = tx._sock
        prev_os = sock.gettimeout()  # type: ignore[union-attr]
        sock.settimeout(seconds)  # type: ignore[union-attr]
    try:
        yield
    finally:
        tx.io_timeout = prev  # type: ignore[attr-defined]
        if isinstance(tx, SerialTransport):
            ser.timeout = prev_os  # type: ignore[union-attr]
        elif isinstance(tx, TcpTransport):
            sock.settimeout(prev_os)  # type: ignore[union-attr]


def _listen(tx: TRTransport, seconds: float, t0: float) -> list[tuple[float, bytes]]:
    """Every byte that arrives within `seconds`, raw, stamped relative to t0."""
    got: list[tuple[float, bytes]] = []
    end = time.monotonic() + seconds
    with _read_timeout(tx, 0.02):
        while time.monotonic() < end:
            try:
                b = tx.recv_exact(1)
            except TRError:
                continue
            got.append((time.monotonic() - t0, b))
    return got


def _show(got: list[tuple[float, bytes]]) -> str:
    if not got:
        return "nothing"
    data = b"".join(b for _, b in got)
    return f"{len(data)} bytes, first at {got[0][0] * 1e3:.0f} ms, last at {got[-1][0] * 1e3:.0f} ms: {data!r}"


def _step(label: str, fn):
    t = time.monotonic()
    try:
        out = fn()
        verdict = f"ok {out.hex(' ') if isinstance(out, bytes) else ''}".rstrip()
    except (OSError, TRError) as e:
        out = None
        verdict = f"{type(e).__name__}: {e}"
    print(f"    {label}: {verdict} ({(time.monotonic() - t) * 1e3:.0f} ms)")
    return out


def _prime(tr: TRClient) -> None:
    tr.write_segment(A, b"\xaa" * 16)
    tr.write_segment(B, b"\xcc" * 8)


def _report_targets(tr: TRClient, a_expect: bytes, b_expect: bytes) -> bool:
    a = _step(f"read ${A:04X}", lambda: tr.read_segment(A, 16))
    b = _step(f"read ${B:04X}", lambda: tr.read_segment(B, 8))
    ok = a == a_expect and b == b_expect
    print(f"    targets as expected for an in-step link: {ok}")
    return ok


def case_short(tr: TRClient) -> bool:
    _prime(tr)
    t0 = time.monotonic()
    tr.transport.send_all(_frame(A, 16, b"\x11" * 8))
    print(f"    firmware said: {_show(_listen(tr.transport, 1.5, t0))}")
    a = _step(f"read ${A:04X}", lambda: tr.read_segment(A, 16))
    vec = _step("read KERNAL reset vector", lambda: tr.read_segment(KERNAL_RESET_VECTOR, 2))
    ok = a == b"\xaa" * 16 and vec is not None
    print(f"    the short frame wrote nothing and the next command is in step: {ok}")
    return ok


def case_swallow(tr: TRClient) -> bool:
    _prime(tr)
    t0 = time.monotonic()
    tr.transport.send_all(_frame(A, 16, b"\x11" * 8) + _frame(B, 8, b"\x22" * 8))
    print(f"    firmware said: {_show(_listen(tr.transport, 1.5, t0))}")
    swallowed = b"\x11" * 8 + _frame(B, 8, b"\x22" * 2)
    a = _step(f"read ${A:04X}", lambda: tr.read_segment(A, 16))
    b = _step(f"read ${B:04X}", lambda: tr.read_segment(B, 8))
    untouched = b == b"\xcc" * 8
    print(f"    ${A:04X} holds the second frame as payload: {a == swallowed}")
    print(f"    ${B:04X} untouched: {untouched}")
    return a == swallowed and untouched


def _cut_then_write(tr: TRClient, idle_s: float) -> bool:
    _prime(tr)
    real = tr.transport.send_all

    def cut_once(data: bytes) -> None:
        tr.transport.send_all = real  # type: ignore[method-assign]
        real(data[:8])
        raise OSError("simulated send failure after 8 bytes")

    tr.transport.send_all = cut_once  # type: ignore[method-assign]
    _step(f"write ${A:04X} (cut)", lambda: tr.write_segment(A, b"\x11" * 16))
    if idle_s:
        print(f"    idle {idle_s:.1f} s")
        time.sleep(idle_s)
    _step(f"write ${B:04X}", lambda: tr.write_segment(B, b"\x22" * 8))
    return _report_targets(tr, b"\xaa" * 16, b"\x22" * 8)


def case_cut_send(tr: TRClient) -> bool:
    return _cut_then_write(tr, 0.0)


def case_cut_idle(tr: TRClient) -> bool:
    return _cut_then_write(tr, 2.0)


def case_late_ack(tr: TRClient) -> bool:
    tr.write_segment(BULK, bytes(8))
    with _read_timeout(tr.transport, 0.001):
        _step(
            f"write ${BULK:04X} x4096 (1 ms timeout)",
            lambda: tr.write_segment(BULK, b"\x33" * 4096),
        )
    got = _step(f"read ${BULK:04X}", lambda: tr.read_segment(BULK, 8))
    vec = _step("read KERNAL reset vector", lambda: tr.read_segment(KERNAL_RESET_VECTOR, 2))
    ok = got == b"\x33" * 8 and vec is not None
    print(f"    the write landed and later commands read their own replies: {ok}")
    return ok


def case_late_read(tr: TRClient) -> bool:
    with _read_timeout(tr.transport, 0.001):
        _step(f"read ${BULK:04X} x4096 (1 ms timeout)", lambda: tr.read_segment(BULK, 4096))
    first = _step("read KERNAL reset vector", lambda: tr.read_segment(KERNAL_RESET_VECTOR, 2))
    again = _step("read KERNAL reset vector", lambda: tr.read_segment(KERNAL_RESET_VECTOR, 2))
    ok = first is not None and first == again
    print(f"    later reads are in step: {ok}")
    return ok


CASES = {
    "short": case_short,
    "swallow": case_swallow,
    "cut-send": case_cut_send,
    "cut-idle": case_cut_idle,
    "late-ack": case_late_ack,
    "late-read": case_late_read,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://")
    ap.add_argument("--case", action="append", choices=sorted(CASES), help="default: all")
    args = ap.parse_args()

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    cfg.teensyrom.dma_slicing = "off"
    be = make_backend(cfg)
    if not isinstance(be, TeensyROMBackend):
        print("[abort] not a TeensyROM URL")
        be.close()
        return 1
    tr = be.tr
    results: dict[str, bool] = {}
    try:
        print(f"firmware: {tr.ping()!r}")
        for name in args.case or list(CASES):
            print(f"\n[{name}]")
            results[name] = CASES[name](tr)
            # Leave the next case a quiet link whatever this one did.
            tr.drain_after_command(1.6)
    finally:
        be.close()
        d.machine_reset(args.url)
    print("\n" + "  ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
