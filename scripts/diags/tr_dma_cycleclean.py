#!/usr/bin/env python3
"""Validate whether a TeensyROM+ firmware build has a *cycle-clean* WriteC64Mem
DMA — i.e. whether the TR can stream memory writes while a normal, IRQ-driven
program runs on the 6510 *without corrupting it*.

Background (see auto-memory project_tr_cycle_clean_dma_review): the TR drives a
real expansion-port `/DMA` pin. The shipping WriteC64Mem path asserts `/DMA`
asynchronously at an arbitrary CPU moment (state DMA_S_StartTransfer). The NMOS
6510 can't be halted mid-write, so an assert landing on/near a CPU write burst
contends the bus -> a garbage opcode fetch / mis-write. On a running BASIC
interpreter that surfaces within seconds as ?SYNTAX ERROR / ?UNDEF'D STATEMENT /
freeze. That is exactly why c64cast does NOT stream over a running interpreter:
its TR bring-up parks the 6510 in a glitch-tolerant NOP-sled spin stub
(teensyrom_api._SPIN_STUB) with IRQs masked. The candidate firmware fix gates
the assert on a safe cycle (VIC badline read / freeze), which should let a
*normal* program survive sustained hammering.

This tool measures that directly, with the Cam Link as the only readback (the TR
protocol has no read-C64-memory token):

  1. Connect over a transport (--tcp HOST and/or --serial PORT), FWCheck + Ping.
  2. Border smoke test: reset -> TR menu, WriteC64Mem a few colours to $D020,
     and capture a frame so you can confirm the border actually changed (proves
     WriteC64Mem lands at all + a byte-swapped address would miss the border).
  3. Heartbeat: PostFile + LaunchFile a one-line BASIC program that cycles the
     border colour forever:  10 FORA=0TO15:POKE53280,A:<delay>:NEXTA:GOTO10
     This is IRQ-driven (kernal jiffy -> the 3-byte stack push worst case),
     interpreter-fragile (a glitch -> error + READY, border stops cycling), and
     self-reporting (border cycles == alive). See _HEARTBEAT_BODY for why it
     uses a bounded FOR variable and avoids the PEEK token.
  4. Hammer: stream WriteC64Mem to a RAM target ($4000) as fast as the acks
     allow, for --seconds, sweeping --sizes. Small segments stress the assertion
     hand-off (many /DMA edges); large segments stress the write-timing margin
     (more bytes per burst).
  5. Analyse the Cam Link capture for border liveness over time: while the
     heartbeat runs the whole-frame mean colour swings hard (border cycling);
     when BASIC dies it freezes. Report time-to-death per size, or "survived".

Old firmware -> heartbeat dies within seconds under hammer (small sizes worst).
Cycle-clean firmware -> heartbeat survives the full window at every size.

    scripts/diags/tr_dma_cycleclean.py --tcp <TR_HOST>
    scripts/diags/tr_dma_cycleclean.py --serial /dev/cu.usbmodem<XXXX>
    scripts/diags/tr_dma_cycleclean.py --tcp <TR_HOST> --serial <PORT> \
        --seconds 30 --sizes 1,256,4096
    # both transports, 30s window, sizes 1/256/4096, capture + analyse.

Always resets the C64 on the way out (the standing silence-and-reset rule).
"""

from __future__ import annotations

import argparse
import contextlib
import threading
import time
from dataclasses import dataclass

import _diaglib as d

from c64cast.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    DRIVE_SD,
    DRIVE_USB,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
)

# ---- C64 targets ----------------------------------------------------------
HAMMER_ADDR = 0x4000  # plain RAM, clear of BASIC, screen, and the heartbeat
BORDER_REG = 0xD020
HEARTBEAT_DEST = "c64cast/hb.prg"  # on TR storage
_LAUNCH_SETTLE_S = 1.2  # let the loader's "RUNNING..." land + the loop start


def _basic_one_liner(line_no: int, body: bytes) -> bytes:
    """Assemble a single-line tokenized BASIC PRG (load address $0801).

    `body` is the already-tokenized line content (keyword tokens + ASCII),
    WITHOUT the line link, line number, or end-of-line null. Layout mirrors
    api._build_basic_sys_stub: load addr, next-line ptr, line number, body,
    EOL null, then the two end-of-program nulls."""
    LOAD_ADDR = 0x0801
    line = body + b"\x00"  # + end-of-line
    next_ptr = LOAD_ADDR + 4 + len(line)  # skip own ptr(2) + line-no(2) + line
    return (
        LOAD_ADDR.to_bytes(2, "little")
        + next_ptr.to_bytes(2, "little")
        + line_no.to_bytes(2, "little")
        + line
        + b"\x00\x00"
    )


# Border heartbeat:  10 FORA=0TO15:POKE53280,A:FORI=0TO200:NEXTI:NEXTA:GOTO10
# The border colour is the outer FOR variable A, so it wraps 0..15 forever — an
# unbounded `A=A+1` counter would hit POKE53280,256 -> ?ILLEGAL QUANTITY ERROR
# and self-terminate (~73 s at this loop rate), a false "death". The PEEK token
# errors out at RUN on this machine for reasons unrelated to the DMA under test,
# so the cycler avoids it; FOR/TO/NEXT/POKE/GOTO are all confirmed to run. The
# inner FOR..NEXT delay steps the border a few times/sec so each captured frame
# catches a distinct colour (a strong alive/frozen signal). Interpreter
# fragility (live FOR stack + variables + loop) is exactly what a non-cycle-clean
# DMA would corrupt.
#   0x81 FOR   0xB2 =   0xA4 TO   0x97 POKE   0x82 NEXT   0x89 GOTO
_HEARTBEAT_BODY = (
    bytes([0x81])
    + b"A"
    + bytes([0xB2])
    + b"0"
    + bytes([0xA4])
    + b"15"  # FORA=0TO15
    + bytes([0x3A, 0x97])
    + b"53280"
    + bytes([0x2C])
    + b"A"  # :POKE53280,A
    + bytes([0x3A, 0x81])
    + b"I"
    + bytes([0xB2])
    + b"0"
    + bytes([0xA4])
    + b"200"  # :FORI=0TO200
    + bytes([0x3A, 0x82])
    + b"I"  # :NEXTI
    + bytes([0x3A, 0x82])
    + b"A"  # :NEXTA
    + bytes([0x3A, 0x89])
    + b"10"  # :GOTO10
)
HEARTBEAT_PRG = _basic_one_liner(10, _HEARTBEAT_BODY)


# ---------------------------------------------------------------------------
# Cam Link capture: a background thread that grabs frames and records only the
# whole-frame mean colour + timestamp (cheap; the border dominates the swing).
# ---------------------------------------------------------------------------
@dataclass
class CamSample:
    t: float
    mean: tuple[float, float, float]  # BGR


class CamCapture:
    def __init__(self, index: int):
        import cv2

        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise SystemExit(
                f"could not open Cam Link cv2 index {index} "
                f"(default {d.CAMLINK_CV2_INDEX}; override --cam / C64_DIAG_CV2)"
            )
        for _ in range(8):  # warm up handshake/exposure
            self.cap.read()
        self.samples: list[CamSample] = []
        self._last_frame = None
        self._run = False
        self._thr: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._run = True
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()

    def _loop(self) -> None:
        while self._run:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                continue
            m = frame.reshape(-1, 3).mean(axis=0)
            with self._lock:
                self._last_frame = frame
                self.samples.append(
                    CamSample(time.monotonic(), (float(m[0]), float(m[1]), float(m[2])))
                )

    def snapshot(self):
        with self._lock:
            return None if self._last_frame is None else self._last_frame.copy()

    def save_frame(self, name: str) -> str | None:
        import cv2

        f = self.snapshot()
        if f is None:
            return None
        path = str(d.stamped(name, "png"))
        cv2.imwrite(path, f)
        return path

    def stop(self) -> None:
        self._run = False
        if self._thr:
            self._thr.join(timeout=2.0)
        self.cap.release()


_WINDOW_S = 1.2  # trailing window over which the border must visit >1 colour
_MIN_ALIVE_RANGE = 8.0  # baseline colour spread below this -> heartbeat not detected


def _means_in(samples: list[CamSample], t0: float, t1: float) -> list[tuple[float, tuple]]:
    return [(s.t, s.mean) for s in samples if t0 <= s.t <= t1]


def _colour_range(means: list[tuple[float, tuple]]) -> float:
    """Largest per-channel (max-min) spread of the frame-mean colour across a
    set of frames. A cycling border sweeps colours -> large spread; a frozen
    border (BASIC died, or never ran) -> ~0, modulo sensor noise."""
    if len(means) < 2:
        return 0.0
    chans = list(zip(*(m[1] for m in means), strict=True))  # (Bs, Gs, Rs)
    return max(max(c) - min(c) for c in chans)


def analyse_window(
    samples: list[CamSample], hammer_t0: float, hammer_t1: float, baseline_t0: float
) -> dict:
    """Did the border heartbeat survive the hammer window?

    Liveness = the border colour keeps *changing*. Because the heartbeat holds
    each colour for several frames, frame-to-frame delta is ~0 most of the time
    even when alive; the robust signal is the colour *spread* over a trailing
    `_WINDOW_S` window (alive: sweeps many colours -> large spread; frozen: ~0).

    The baseline window [baseline_t0, hammer_t0) sets the 'alive' spread. If the
    baseline spread is itself tiny the test is INVALID (heartbeat never ran /
    capture not seeing it) — reported as ok=False, never as survived/died.

    Death = the trailing-window spread collapses below threshold and stays
    collapsed to the end of the hammer window (the point of no return). Detected
    death lags the true freeze by up to ~`_WINDOW_S` (the window must flush its
    pre-freeze colours)."""
    base = _means_in(samples, baseline_t0, hammer_t0)
    ham = _means_in(samples, hammer_t0, hammer_t1)
    base_range = _colour_range(base)
    if not ham:
        return {"ok": False, "reason": "no frames captured in hammer window"}
    if base_range < _MIN_ALIVE_RANGE:
        return {
            "ok": False,
            "reason": f"heartbeat not detected in baseline (colour spread "
            f"{base_range:.1f} < {_MIN_ALIVE_RANGE}); border not cycling / capture blind",
            "base_range": base_range,
        }

    thresh = max(4.0, 0.35 * base_range)
    # Per-frame 'active' = colour spread over the trailing _WINDOW_S is alive.
    active: list[tuple[float, bool]] = []
    for t, _ in ham:
        win = [m for m in ham if t - _WINDOW_S <= m[0] <= t]
        if len(win) < 3:
            continue
        active.append((t - hammer_t0, _colour_range(win) >= thresh))

    death: float | None = None
    for i, (rel_t, is_active) in enumerate(active):
        if not is_active and all(not a for _, a in active[i:]):
            death = rel_t  # first frame after which it never recovers
            break

    return {
        "ok": True,
        "base_range": base_range,
        "hammer_range": _colour_range(ham),
        "threshold": thresh,
        "survived": death is None,
        "death_rel_s": death,
        "n_frames": len(ham),
    }


# ---------------------------------------------------------------------------
# TR control helpers
# ---------------------------------------------------------------------------
def connect(transport_desc: str, *, tcp_host: str | None, serial_port: str | None) -> TRClient:
    if transport_desc == "tcp":
        tx = TcpTransport(tcp_host, DEFAULT_TCP_PORT)  # type: ignore[arg-type]
    else:
        tx = SerialTransport(serial_port, DEFAULT_BAUD)  # type: ignore[arg-type]
    client = TRClient(tx)
    client.connect()  # raises TRError on a bad link
    return client


def upload_and_launch(
    client: TRClient, prg: bytes, dest: str, drive: int, attempts: int = 6
) -> None:
    """delete-then-PostFile (PostFile refuses overwrite) + LaunchFile, with the
    same post-reset retry the backend uses (menu/SD not ready immediately)."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            # missing file -> delete fails, which is expected (PostFile won't
            # overwrite, so we delete first; nothing to delete is fine).
            with contextlib.suppress(OSError, TRError):
                client.delete_file(dest, drive)
            client.post_file(prg, dest, drive)
            client.launch_file(dest, drive)
            if i > 1:
                print(f"      (launched on attempt {i})")
            return
        except (OSError, TRError) as e:
            last = e
            time.sleep(1.0)
    raise TRError(f"upload+launch {dest!r} failed after {attempts} attempts: {last}")


def hammer(client: TRClient, addr: int, size: int, seconds: float) -> dict:
    """Stream WriteC64Mem of `size` bytes to `addr` for `seconds`. Returns
    throughput stats. A transport error mid-hammer is recorded, not raised
    (the firmware may drop the link on a bad assert)."""
    payload = bytes((i & 0xFF) for i in range(size))
    n = 0
    err: str | None = None
    t0 = time.monotonic()
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        try:
            client.write_segment(addr, payload)
            n += 1
        except (OSError, TRError) as e:
            err = str(e)
            break
    dt = time.monotonic() - t0
    return {
        "writes": n,
        "bytes": n * size,
        "secs": dt,
        "writes_per_s": n / dt if dt else 0.0,
        "kib_per_s": n * size / 1024 / dt if dt else 0.0,
        "error": err,
    }


# ---------------------------------------------------------------------------
# Test stages
# ---------------------------------------------------------------------------
def run_transport(label: str, client: TRClient, cam: CamCapture, args) -> list[dict]:
    print(f"\n=== Transport: {label} ===")
    print(f"  connected via {client.transport.description}; firmware: {client.firmware}")
    ping = client.ping()
    if ping:
        print(f"  ping: {ping!r}")

    drive = DRIVE_SD if args.storage == "sd" else DRIVE_USB

    # ---- (2) border smoke test ----
    print("  [smoke] reset -> TR menu, then write $D020 colours ...")
    client.reset()
    time.sleep(args.reset_settle)
    # Drain post-reset boot chatter (the TR emits GoodSIDToken 0x9B81 / banner
    # text after SID detection) before raw write_segments — the write hot path
    # deliberately doesn't drain, so a stale token would be misread as the ack.
    client._drain_stale(0.4)
    pre = cam.save_frame(f"{label}_smoke_pre")
    for color in (2, 5, 7, 6):  # red, green, yellow, blue
        client.write_segment(BORDER_REG, bytes([color]))
        time.sleep(0.4)
    post = cam.save_frame(f"{label}_smoke_post")
    print(f"      frames: pre={pre}  post={post}  (border should be blue/6 in post)")

    results: list[dict] = []
    for size in args.sizes:
        print(f"\n  [hammer] segment size {size} B for {args.seconds:.0f}s -> ${HAMMER_ADDR:04X}")
        # Relaunch a fresh heartbeat for each size (a death kills the prior one).
        print("      launching border heartbeat ...")
        client.reset()
        time.sleep(args.reset_settle)
        upload_and_launch(client, HEARTBEAT_PRG, HEARTBEAT_DEST, drive)
        time.sleep(_LAUNCH_SETTLE_S)
        cam.save_frame(f"{label}_hb_running_{size}")

        baseline_t0 = time.monotonic()
        time.sleep(args.baseline)  # measure the 'alive' swing before hammering

        h_t0 = time.monotonic()
        stats = hammer(client, HAMMER_ADDR, size, args.seconds)
        h_t1 = time.monotonic()
        time.sleep(0.6)  # let the capture catch the final state
        end_frame = cam.save_frame(f"{label}_after_{size}")

        verdict = analyse_window(cam.samples, h_t0, h_t1, baseline_t0)
        row = {"transport": label, "size": size, **stats, **verdict, "end_frame": end_frame}
        results.append(row)
        _print_row(row)
    return results


def _print_row(r: dict) -> None:
    thru = f"{r['writes']} writes / {r['kib_per_s']:.0f} KiB/s / {r['writes_per_s']:.0f} w/s"
    if r.get("error"):
        print(f"      transport ERROR mid-hammer: {r['error']}")
    if not r.get("ok"):
        print(f"      INVALID ⚠️  {r.get('reason')}  ({thru})")
        return
    base, ham = r["base_range"], r["hammer_range"]
    if r["survived"]:
        print(
            f"      SURVIVED ✅  border cycled through {r['secs']:.0f}s of hammer  "
            f"(spread base {base:.0f} / hammer {ham:.0f}; {thru})"
        )
    else:
        print(
            f"      DIED ❌  border froze ~+{r['death_rel_s']:.1f}s  "
            f"(spread base {base:.0f} / hammer {ham:.0f}; {thru})"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", default=None, help="run over TCP to this TR host")
    ap.add_argument("--serial", metavar="PORT", default=None, help="run over this serial device")
    ap.add_argument(
        "--seconds", type=float, default=20.0, help="hammer window per size (default 20)"
    )
    ap.add_argument(
        "--baseline",
        type=float,
        default=2.0,
        help="seconds of pre-hammer heartbeat to set the alive baseline (default 2)",
    )
    ap.add_argument(
        "--sizes",
        default="1,256,4096",
        help="comma-list of WriteC64Mem segment sizes to sweep (default 1,256,4096)",
    )
    ap.add_argument(
        "--storage",
        choices=("sd", "usb"),
        default="sd",
        help="TR storage for the heartbeat PRG (default sd)",
    )
    ap.add_argument(
        "--cam",
        type=int,
        default=d.CAMLINK_CV2_INDEX,
        help=f"Cam Link cv2 index (default {d.CAMLINK_CV2_INDEX})",
    )
    ap.add_argument(
        "--reset-settle",
        type=float,
        default=3.0,
        help="seconds to wait after reset for the TR menu/SD (default 3)",
    )
    ap.add_argument("--no-reset-exit", action="store_true", help="skip the final reset")
    args = ap.parse_args()

    if not args.tcp and not args.serial:
        ap.error("specify at least one of --tcp HOST / --serial PORT")
    args.sizes = [int(s) for s in args.sizes.split(",")]

    print("Opening Cam Link ...")
    cam = CamCapture(args.cam)
    cam.start()
    time.sleep(0.5)

    all_results: list[dict] = []
    clients: list[TRClient] = []
    try:
        for label, kind, val in (("tcp", "tcp", args.tcp), ("serial", "serial", args.serial)):
            if not val:
                continue
            try:
                client = connect(kind, tcp_host=args.tcp, serial_port=args.serial)
            except TRError as e:
                print(f"\n=== Transport: {label} ===\n  CONNECT FAILED: {e}")
                continue
            clients.append(client)
            # A failure on one transport (e.g. serial that opens but never
            # acks) must not abort the other transport, the summary, or the
            # end-of-run reset.
            try:
                all_results.extend(run_transport(label, client, cam, args))
            except (OSError, TRError) as e:
                print(f"  {label}: ABORTED after transport error: {e}")
    finally:
        for c in clients:
            if not args.no_reset_exit:
                with contextlib.suppress(OSError, TRError):
                    c.reset()
            c.close()
        cam.stop()

    _summary(all_results)
    # exit 0 = all survived; 1 = at least one death; 2 = nothing ran
    if not all_results:
        return 2
    return 0 if all(r.get("survived") for r in all_results if r.get("ok")) else 1


def _summary(results: list[dict]) -> None:
    print("\n===================== SUMMARY =====================")
    if not results:
        print("no hammer windows ran.")
        return
    print(f"{'transport':>9}  {'size':>5}  {'verdict':>9}  {'death':>7}  {'throughput':>20}")
    for r in results:
        if not r.get("ok"):
            verdict, death = "n/a", "-"
        elif r["survived"]:
            verdict, death = "SURVIVED", "-"
        else:
            verdict, death = "DIED", f"+{r['death_rel_s']:.1f}s"
        thru = f"{r['kib_per_s']:.0f} KiB/s"
        print(f"{r['transport']:>9}  {r['size']:>5}  {verdict:>9}  {death:>7}  {thru:>20}")
    survived = [r for r in results if r.get("ok") and r["survived"]]
    died = [r for r in results if r.get("ok") and not r["survived"]]
    print("\n--- verdict ---")
    if died and not survived:
        print(
            "NOT cycle-clean ❌  the heartbeat died under hammer in every window "
            "(matches the old async DMA_S_StartTransfer behaviour)."
        )
    elif survived and not died:
        print(
            "CYCLE-CLEAN ✅  the heartbeat survived sustained hammering at every "
            "size/transport — a normal IRQ-driven program is no longer corrupted."
        )
    else:
        print(
            "PARTIAL ⚠️  survived some windows, died in others — note which "
            "size/transport failed (small sizes stress the assertion hand-off, "
            "large sizes the write-timing margin)."
        )


if __name__ == "__main__":
    raise SystemExit(main())
