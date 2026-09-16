#!/usr/bin/env python3
"""Launch the C128-mode VDC cartridge on real hardware and measure the resident
blit path — the thing that makes a VDC display target viable.

Everything up to now drove the VDC from the host, one porthole access per byte,
at 5.5 KB/s. This launches ``hw/vdc_rom.py``'s cartridge instead: the C128 boots
into **native 128 mode**, the resident 8502 loop takes over, and the host's job
becomes a bulk DMA into C128 RAM plus a one-byte command. The 8502 does the
porthole work locally.

**No capture card needed for the pass/fail parts.** The ROM increments a
heartbeat byte in the mailbox on every idle pass, so a pair of reads proves the
cartridge booted and the loop is running. Stages 1-5 are all self-verifying that
way; only ``--pattern`` needs a human looking at the RGBI monitor.

    scripts/diags/vdc_c128.py --serial /dev/cu.usbmodemXXXX
    scripts/diags/vdc_c128.py --serial <PORT> --pattern image --image pic.jpg
    scripts/diags/vdc_c128.py --serial <PORT> --pattern animate --hold 30

Stages
  1. build + upload + launch the .crt, confirm the heartbeat is moving
  2. read back the boot state (VDC registers, cleared VRAM) through the porthole
  3. host -> C128 RAM DMA rate (the TeensyROM+ link's bulk path)
  4. RAM -> VRAM blit rate (the resident loop's inner loop)
  5. end-to-end frame time and the frame rate that implies
 5b. how much of the porthole's wait is display fetches (R1 = 0)
 5c. optional attribute block height sweep (R9) -- needs eyes on the monitor
  6. optional visible pattern: image | animate | palette | none

Stages 3-5 are skippable with --pattern-only, for runs whose point is the
picture rather than the numbers.

Leaves the C128 reset on the way out (the standing silence-and-reset rule),
unless --no-reset-exit.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from typing import Final

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)

from c64cast.hw import vdc, vdc_rom
from c64cast.hw.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
)

UPLOAD_PATH = "/c64cast/vdcrom.crt"


def connect(*, tcp: str | None, serial: str | None) -> TRClient:
    if tcp:
        tx = TcpTransport(tcp, DEFAULT_TCP_PORT)
    elif serial:
        tx = SerialTransport(serial, DEFAULT_BAUD)
    else:  # pragma: no cover - argparse guards this
        raise SystemExit("specify --tcp HOST or --serial PORT")
    client = TRClient(tx)
    client.connect()
    return client


def make_porthole(client: TRClient) -> vdc.VdcPorthole:
    """The host's own porthole, for reading VDC state back while the resident
    loop is idle. Safe only because the idle loop touches nothing but RAM."""

    def write(addr: int, data: bytes) -> None:
        client.write_segment(addr, data)

    def read(addr: int, n: int) -> bytes | None:
        try:
            return client.read_segment(addr, n)
        except (OSError, TRError):
            return None

    return vdc.VdcPorthole(write, read)


# ---------------------------------------------------------------------------
# the mailbox protocol
# ---------------------------------------------------------------------------


def issue(
    client: TRClient, cmd: int, *, arg: int = 0, dst: int = 0, count: int = 0, src: int = 0
) -> int:
    """Stage the parameters, then commit with the command byte in its own
    write. Returns the MAIL_DONE value from before the command, for wait_done.

    The two writes are not an accident: the command byte is the commit, and
    bundling it with the parameters would let the loop see a command whose
    parameters had only partly landed.

    The parameter block is read back and rewritten until it sticks: a wedge
    probe caught a blit running with dst/src/count that the host DMA had
    corrupted in flight (bit 0 of dst and src both flipped 0->1), which sends
    the blit to a wild address and hangs the VDC. Reading back before the
    commit keeps a corrupted block from ever being executed."""
    before = client.read_segment(vdc_rom.MAIL_DONE, 1)[0]
    params = bytes([arg, dst & 0xFF, dst >> 8, count & 0xFF, count >> 8, src & 0xFF, src >> 8])
    back = b""
    for attempt in range(4):
        client.write_segment(vdc_rom.MAIL_ARG, params)
        back = client.read_segment(vdc_rom.MAIL_ARG, len(params))
        if back == params:
            break
        print(
            f"    issue: command block corrupted in DMA (try {attempt + 1}): "
            f"{params.hex()} -> {back.hex()}"
        )
    else:
        raise TRError(f"command block will not stick: {params.hex()} vs {back.hex()}")
    client.write_segment(vdc_rom.MAIL_CMD, bytes([cmd]))
    return before


#: Gap between MAIL_DONE polls while a command runs. **Not a politeness knob.**
#: Every DMA read halts the 8502, so polling while a command runs steals cycles
#: from the command being waited on. A no-sleep poll is fatal: a 24000-byte blit
#: that finishes in ~0.9 s under a sleeping poll does not finish in 15 s under a
#: tight one. 5 ms is still inside the damage, just less obviously — an A/B of
#: eight 4096-byte blits each hung twice at 5 ms against once at 50 ms, and left
#: two of three completions corrupt against four of seven clean. The blit itself
#: takes the same ~230 ms either way, so the only thing a shorter gap buys is
#: timing resolution, and 50 ms against 230 is resolution enough.
POLL_INTERVAL: Final = 0.050


def probe_wedge(client: TRClient) -> None:
    """Interrogate a resident loop that stopped acknowledging. The bus is still
    the Teensy's while the 8502 is halted, and every read here freezes it for
    one DMA/porthole cycle and lets it resume exactly where it stalled — so this
    is the one look at the wedge's internals that exists before a reset erases
    it. Read-only; the caller resets afterward regardless."""
    print("\n  --- WEDGE PROBE (resident loop stopped ack'ing) ---")

    cmd, arg, dst_lo, dst_hi, cnt_lo, cnt_hi, src_lo, src_hi, beat, done = client.read_segment(
        vdc_rom.MAILBOX, 10
    )
    dst = dst_lo | (dst_hi << 8)
    cnt = cnt_lo | (cnt_hi << 8)
    src = src_lo | (src_hi << 8)
    print(
        f"  mailbox: cmd=${cmd:02x} arg=${arg:02x} done={done} beat={beat}  "
        f"dst=${dst:04x} cnt={cnt} src=${src:04x}"
    )

    time.sleep(0.15)
    beat2 = client.read_segment(vdc_rom.MAIL_BEAT, 1)[0]
    moving = beat2 != beat
    print(
        f"  idle beat {beat} -> {beat2}: "
        + (
            "RUNNING — the command returned but never bumped MAIL_DONE"
            if moving
            else "FROZEN — the 8502 is spinning inside the command"
        )
    )

    # The blit's live cursor lives in zero page: $FB/$FC source pointer,
    # $FD remainder-low, $FE full-pages-left (vdc_rom.resident_source, blit:).
    sp_lo, sp_hi, rem_lo, pages_left = client.read_segment(0x00FB, 4)
    sptr = sp_lo | (sp_hi << 8)
    if cmd == vdc_rom.CMD_BLIT and cnt:
        did = sptr - src
        print(
            f"  blit cursor: src ptr ${sptr:04x} -> {did} of {cnt} bytes "
            f"({100 * did // cnt if cnt else 0}%), pages_left={pages_left} rem_lo={rem_lo}"
        )

    port = make_porthole(client)
    statuses = [port.read_status() for _ in range(5)]
    ready = sum(1 for st in statuses if st is not None and st & vdc.STATUS_READY)
    shown = " ".join("??" if st is None else f"{st:02x}" for st in statuses)
    print(f"  $D600 x5: {shown}   ready-bit set {ready}/5")
    if ready == 0:
        print("    -> VDC never signals ready: the chip stopped completing accesses")
    elif ready == 5:
        print("    -> VDC is ready every read: the 8502's BIT $D600 poll is the thing stuck")

    r18, r19 = port.read_reg(vdc.R.UPDATE_HI), port.read_reg(vdc.R.UPDATE_LO)
    if r18 is not None and r19 is not None:
        wptr = (r18 << 8) | r19
        tail = f" -> {wptr - dst} past dst ${dst:04x}" if cmd == vdc_rom.CMD_BLIT else ""
        print(f"  VDC write ptr R18/R19: ${wptr:04x}{tail}")
    print("  --- end probe ---")


def wait_done(client: TRClient, before: int, timeout: float = 20.0, *, probe: bool = True) -> float:
    """Seconds until the resident loop acknowledged, +/- POLL_INTERVAL. On
    timeout, probe the halted machine (unless ``probe`` is cleared) before
    raising — see :func:`probe_wedge`."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if client.read_segment(vdc_rom.MAIL_DONE, 1)[0] != before:
            return time.perf_counter() - t0
        time.sleep(POLL_INTERVAL)
    if probe:
        with contextlib.suppress(OSError, TRError):
            probe_wedge(client)
    raise TimeoutError(f"resident loop never acknowledged command (>{timeout}s)")


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_launch(client: TRClient, settle: float) -> bool:
    print("\n[1] build + upload + launch the C128-mode cartridge")
    crt = vdc_rom.build_crt()
    used = len(vdc_rom.assemble(vdc_rom.resident_source(), vdc_rom.RESIDENT_ADDR))
    print(f"    .crt {len(crt)} B   resident loop {used} B of {vdc_rom.RESIDENT_MAX}")

    print("    resetting to the TR menu ...")
    client.reset()
    time.sleep(settle)
    client._drain_stale(0.4)

    with contextlib.suppress(OSError, TRError):
        client.delete_file(UPLOAD_PATH)
    client.post_file(crt, UPLOAD_PATH)
    client.launch_file(UPLOAD_PATH)
    # LaunchFile acks and then streams console text; draining to silence is the
    # only safe way to know the link is ours again.
    client.drain_after_command(0.6)
    time.sleep(settle)

    print("    checking the heartbeat (proof the loop is running) ...")
    for attempt in range(12):
        try:
            a = client.read_segment(vdc_rom.MAIL_BEAT, 1)
            time.sleep(0.2)
            b = client.read_segment(vdc_rom.MAIL_BEAT, 1)
        except (OSError, TRError) as e:
            print(f"      attempt {attempt + 1}: link not answering yet ({e})")
            time.sleep(0.5)
            continue
        if a != b:
            print(f"    heartbeat moving: ${a[0]:02X} -> ${b[0]:02X}   ALIVE")
            print("    -> the 80-col screen should now be SOLID CYAN.")
            return True
        time.sleep(0.4)
    print("    heartbeat NOT moving. The cartridge did not boot, or DMA cannot")
    print("    reach C128 RAM while a C128-mode cart is running.")
    return False


def blank_screen(port: vdc.VdcPorthole) -> None:
    """Leave the 80-column display solid black.

    Nothing reprograms the VDC on the way out: a reset drops the machine into
    the TeensyROM menu, which is a 40-column screen, so whatever register
    program and video RAM this run left behind stay on the RGBI output and show
    as a flickering picture. Turning per-cell attributes off hands the whole
    screen to R26, so black on black there is one write rather than an
    8000-byte attribute fill."""
    print("\nblanking the 80-column screen ...")
    port.write_reg(vdc.R.H_SCROLL_CTRL, vdc.H_SCROLL_BITMAP_BIT | vdc.H_SCROLL_NEUTRAL)
    port.write_reg(vdc.R.FG_BG_COLOR, 0x00)


def stage_boot_state(port: vdc.VdcPorthole) -> None:
    print("\n[2] boot state, read back through the porthole")
    # R28's unused low bits read back as 1s on an 8563 R8/R9, so a written $18
    # reads as $3F. Compare only the bit that carries meaning here: bit 4, the
    # 64 KiB address select. The charset-base bits above it are don't-care in
    # bitmap mode.
    checks = [
        (vdc.R.V_DISPLAYED, "R6  rows displayed", 0xFF),
        (vdc.R.H_SCROLL_CTRL, "R25 bitmap/attr/hscroll", 0xFF),
        (vdc.R.CHARSET_ADDR, "R28 64K address select", 0x10),
    ]
    for reg, label, mask in checks:
        want = vdc.BITMAP_640x200_REGS[reg] & mask
        got = port.read_reg(reg)
        ok = got is not None and (got & mask) == want
        state = "OK" if ok else f"MISMATCH (wanted ${want:02X} under ${mask:02X})"
        got_text = "unreadable" if got is None else f"${got:02X}"
        print(f"    {label:26s} {got_text:>10s}  {state}")

    bitmap = port.read_ram(vdc.BITMAP_BASE, 64)
    attr = port.read_ram(vdc.ATTR_BASE, 64)
    print(f"    bitmap cleared to zero     {'OK' if bitmap == bytes(64) else 'NO'}")
    ok_attr = attr == bytes([vdc_rom.ATTR_INIT]) * 64
    print(f"    attributes flat-filled     {'OK' if ok_attr else 'NO'}")


def stage_dma_rate(client: TRClient, nbytes: int) -> bytes:
    print(f"\n[3] host -> C128 RAM DMA rate ({nbytes} B)")
    payload = bytes((i * 37) & 0xFF for i in range(nbytes))
    t0 = time.perf_counter()
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, payload)
    # A read cannot be answered until everything queued ahead of it has been
    # processed, so it is what turns a buffered send into a measured transfer.
    back = client.read_segment(vdc_rom.FRAMEBUF_ADDR, 64)
    dt = time.perf_counter() - t0
    ok = back == payload[:64]
    print(f"    {nbytes / dt:9.0f} B/s   ({dt:.3f}s)   readback {'OK' if ok else 'MISMATCH'}")

    # Verifying the whole staged buffer, not just the flushing read, is what
    # keeps a staging fault from being scored against the VDC: the blit is
    # checked against the host's copy of the payload, so a bit the DMA dropped
    # on the way into $4000 gets faithfully copied to VRAM and read back as a
    # VDC error. DMA writes are fire-and-forget, and this is the only place
    # anything looks at what actually landed.
    staged = client.read_segment(vdc_rom.FRAMEBUF_ADDR, nbytes)
    bad = [i for i in range(nbytes) if staged[i] != payload[i]]
    if not bad:
        print(f"    staged: all {nbytes} B intact")
    else:
        bits = sum(bin(staged[i] ^ payload[i]).count("1") for i in bad)
        drop = sum(bin(payload[i] & ~staged[i]).count("1") for i in bad)
        print(
            f"    staged: {len(bad)} of {nbytes} B WRONG ({bits} bits, "
            f"{drop} of them 1->0), first at {bad[0]} "
            f"({payload[bad[0]]:#04x} -> {staged[bad[0]]:#04x})"
        )
        print("            blit verification below is unusable; fix DMA first")
    return payload


def stage_blit_rate(client: TRClient, port: vdc.VdcPorthole, payload: bytes) -> float:
    del port  # verify/repair build their own; kept in the signature for callers
    nbytes = len(payload)
    print(f"\n[4] RAM -> VRAM blit rate ({nbytes} B, resident 8502 loop)")
    before = issue(
        client, vdc_rom.CMD_BLIT, dst=vdc.BITMAP_BASE, count=nbytes, src=vdc_rom.FRAMEBUF_ADDR
    )
    dt = wait_done(client, before)
    print(f"    {nbytes / dt:9.0f} B/s   ({dt * 1000:.0f} ms +/- {POLL_INTERVAL * 1000:.0f} ms)")
    print(f"    -> versus 5500 B/s host-driven: {(nbytes / dt) / 5500:.0f}x")

    # A blit timed but not read back can clock a corrupt frame as a fast one.
    # Read the whole region twice (a single read is ~1-in-20 lossy) and count
    # only the bytes that stably disagree.
    try:
        bad, got = _stable_diff(client, vdc.BITMAP_BASE, payload)
    except TRError:
        print("    verify: VRAM UNREADABLE")
        return dt
    if not bad:
        print("    verify: every byte landed")
        return dt
    bits = sum(bin(got[i] ^ payload[i]).count("1") for i in bad)
    drops = sum(bin(payload[i] & ~got[i]).count("1") for i in bad)
    print(
        f"    verify: {len(bad)} of {nbytes} bytes wrong "
        f"({bits} bits, {drops} of them 1->0), first at {bad[0]}"
    )
    print(f"            offsets: {bad[:10]}{' ...' if len(bad) > 10 else ''}")
    residual, wedged = _repair(client, payload, bad=bad)
    if not wedged:
        print(
            f"    repair: {'clean' if not residual else f'{len(residual)} still wrong: {residual[:10]}'}"
        )
    return dt


def _truth_read(port: vdc.VdcPorthole, base: int, nbytes: int) -> tuple[bytes, set[int]]:
    """Read VRAM twice and return the bytes both reads agree on, plus the
    offsets where they disagreed. ``read_ram`` is ~1-in-20 lossy per its own
    docstring, so a single read cannot be trusted for a decay hunt — a
    disagreement is read-path noise and gets excluded from the comparison."""
    a = port.read_ram(base, nbytes)
    b = port.read_ram(base, nbytes)
    if a is None or b is None:
        raise TRError("VRAM unreadable")
    return a, {i for i in range(nbytes) if a[i] != b[i]}


def _stable_diff(client: TRClient, base: int, ref: bytes) -> tuple[list[int], bytes]:
    """Offsets where VRAM stably disagrees with ``ref`` (read twice; a
    disagreement between the two reads is read-path noise, not a bad byte),
    plus the bytes actually read for whoever wants the bit direction."""
    port = make_porthole(client)
    got, noise = _truth_read(port, base, len(ref))
    bad = [i for i in range(len(ref)) if i not in noise and got[i] != ref[i]]
    return bad, got


def _spans(offsets: list[int], gap: int = 8) -> list[tuple[int, int]]:
    """Coalesce sorted offsets into (start, stop) spans, merging any pair
    closer than ``gap`` — re-blitting a few good bytes between two drops is
    cheaper than a second mailbox round-trip."""
    spans: list[list[int]] = []
    for o in offsets:
        if spans and o - spans[-1][1] < gap:
            spans[-1][1] = o + 1
        else:
            spans.append([o, o + 1])
    return [(a, b) for a, b in spans]


def _repair(
    client: TRClient,
    payload: bytes,
    *,
    base: int = vdc.BITMAP_BASE,
    rounds: int = 1,
    bad: list[int] | None = None,
) -> tuple[list[int], bool]:
    """Re-blit the bytes the porthole dropped, up to ``rounds`` passes. The
    framebuffer at $4000 still holds ``payload``, so a span re-blit is just
    another CMD_BLIT with shifted dst/src. Pass ``bad`` when the caller has
    already read the region back, to skip the opening re-read. Returns
    (offsets still bad, wedged).

    Each span is one command, and the wedge probe has repeatedly caught the
    8502 crashing partway through a long run of them (~1-2% per command, so a
    50-span repair almost always trips it). A wedge, or a command block the
    host DMA will not land, is caught here rather than aborting the run: the
    caller gets what landed plus a flag, and the machine still needs a reset."""
    fixed = 0
    try:
        if bad is None:
            bad, _ = _stable_diff(client, base, payload)
        for _ in range(rounds):
            if not bad:
                break
            time.sleep(0.15)
            for start, stop in _spans(bad):
                b = issue(
                    client,
                    vdc_rom.CMD_BLIT,
                    dst=base + start,
                    count=stop - start,
                    src=vdc_rom.FRAMEBUF_ADDR + start,
                )
                wait_done(client, b, timeout=10.0)
                fixed += 1
                time.sleep(0.05)
            bad, _ = _stable_diff(client, base, payload)
        return bad, False
    except (TimeoutError, TRError) as e:
        print(f"    repair: aborted after {fixed} spans ({e}) -- reset needed")
        return [], True


def stage_vram_dwell(port: vdc.VdcPorthole, nbytes: int, schedule: tuple[int, ...]) -> None:
    """Blit nothing, write nothing — just watch a region of VRAM for the whole
    schedule. If bytes rot while idle, the ~1-per-1000 blit loss is storage
    (DRAM refresh, R36); if the region holds, the loss is in the write path and
    refresh is a red herring. Those two stories are otherwise indistinguishable."""
    print(f"\n[4b] VRAM dwell — does the image rot while nothing writes it? ({nbytes} B)")
    schedule = tuple(sorted(schedule))  # samples are cumulative from t0
    ref, ref_noise = _truth_read(port, vdc.BITMAP_BASE, nbytes)
    print(f"    t=0    reference latched, {len(ref_noise)} noisy offsets excluded")
    if len(ref_noise) > nbytes // 100:
        # A healthy porthole disagrees with itself on a handful of bytes at
        # most. Hundreds means the VDC is in a bad state (a wedged 8502 upstream
        # scribbled on it), and every "drift" below would be read garbage, not
        # decay. Nothing to learn here until the machine is reset.
        print("    -> porthole is returning garbage; machine is wedged. Skipping.")
        return
    t0 = time.perf_counter()
    prev_bad: set[int] = set()
    for target in schedule:
        while time.perf_counter() - t0 < target:
            time.sleep(0.2)
        now, noise = _truth_read(port, vdc.BITMAP_BASE, nbytes)
        skip = ref_noise | noise
        bad = {i for i in range(nbytes) if i not in skip and now[i] != ref[i]}
        drops = sum(bin(ref[i] & ~now[i]).count("1") for i in bad)
        sets_ = sum(bin(now[i] & ~ref[i]).count("1") for i in bad)
        print(
            f"    t={target:<4d} drifted {len(bad):3d} B  (1->0 {drops}, 0->1 {sets_})  "
            f"new since last {len(bad - prev_bad)}"
        )
        prev_bad = bad
    if not prev_bad:
        print("    -> VRAM held; the loss is in the WRITE path, not storage")
    else:
        print(f"    -> {len(prev_bad)} B rotted in place while idle: storage/refresh, tune R36")


def stage_end_to_end(client: TRClient) -> None:
    print(f"\n[5] end-to-end frame ({vdc.FRAME_BYTES} B: DMA + blit)")
    bitmap = bytes((i * 11) & 0xFF for i in range(vdc.BITMAP_BYTES))
    attr = bytes([0x60]) * vdc.ATTR_BYTES

    t0 = time.perf_counter()
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, bitmap + attr)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.FRAME_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)
    dt = time.perf_counter() - t0

    residual, wedged = _repair(client, bitmap + attr)
    if not wedged:
        print(f"    repair: {'clean' if not residual else f'{len(residual)} still wrong'}")
    print(f"    {dt:.2f}s per frame  ->  {1 / dt:.2f} fps")
    print(
        f"    (attribute plane only, {vdc.ATTR_BYTES} B: "
        f"~{1 / (dt * vdc.ATTR_BYTES / vdc.FRAME_BYTES):.1f} fps)"
    )


# ---------------------------------------------------------------------------
# register experiments
# ---------------------------------------------------------------------------


def stage_blank_gain(client: TRClient, port: vdc.VdcPorthole, payload: bytes) -> None:
    """Blit the same payload twice -- display live, then R1 = 0 -- and report how
    much of the porthole's cost was display fetches.

    A porthole byte costs ~55.5 cycles, of which ~14 are the 8502's own loop (an
    unpolled blit runs at 14.1) and the rest is the 8502 parked in
    ``BIT $D600 / BPL`` waiting for the 8563's scheduler to spare it a slot. R1
    is characters displayed per row, so R1 = 0 fetches no display data at all and
    leaves the scheduler almost nothing else to serve. The gap between the two
    rates is the part of the porthole that is contention rather than silicon.

    The verify is not a formality here. R36 is 0 under this register program, so
    display fetches are the only thing refreshing VRAM; blanking removes them,
    and a blank blit that comes back fast but wrong says the headroom is not
    spendable rather than free."""
    nbytes = len(payload)
    live_r1 = vdc.BITMAP_640x200_REGS[vdc.R.H_DISPLAYED]
    print(f"\n[5b] display-fetch contention (R1 = 0 against R1 = {live_r1})")

    def timed_blit(label: str) -> tuple[float, int]:
        before = issue(
            client, vdc_rom.CMD_BLIT, dst=vdc.BITMAP_BASE, count=nbytes, src=vdc_rom.FRAMEBUF_ADDR
        )
        dt = wait_done(client, before, timeout=30.0)
        try:
            bad, _ = _stable_diff(client, vdc.BITMAP_BASE, payload)
        except TRError:
            bad = [-1]
        rate = nbytes / dt
        verdict = "clean" if not bad else f"{len(bad)} B WRONG"
        print(
            f"    {label:9s} {rate:8.0f} B/s   {1e6 / rate:5.1f} us/B "
            f"(~{1e6 / rate:.0f} cycles at 1 MHz)   {verdict}"
        )
        return dt, len(bad)

    live, live_bad = timed_blit("live")
    try:
        port.write_reg(vdc.R.H_DISPLAYED, 0)
        blank, blank_bad = timed_blit("blanked")
    finally:
        port.write_reg(vdc.R.H_DISPLAYED, live_r1)

    gain = live / blank if blank else 0.0
    saved = 1.0 - blank / live if live else 0.0
    print(f"    -> blanking is {gain:.2f}x; display fetches were {saved * 100:.0f}% of the wait")
    if blank_bad and not live_bad:
        print("       but only the blanked blit corrupted: no refresh without display")
        print("       fetches (R36 = 0), so this headroom is not spendable as-is")
    elif gain < 1.15:
        print("       the wait is not the display -- it is the 8563's own floor,")
        print("       and no host-side scheduling trick will move it")


#: Vertical timing per attribute-block height, holding 264 total scanlines and
#: 200 displayed. R9 is scanlines per character row, and in bitmap mode it sets
#: the attribute block height with it -- so the dial that gave us 8x2 color runs
#: the other way to shrink the attribute plane. Rows are 264/(R9+1) total and
#: 200/(R9+1) displayed; R7 holds sync at the same fraction of the frame
#: (116/132) it sits at in :data:`vdc.BITMAP_640x200_REGS`.
ATTR_HEIGHT_TIMING: Final = {
    # R9: (R4 v_total, R6 v_displayed, R7 v_sync_pos)
    1: (131, 100, 116),
    3: (65, 50, 58),
    7: (32, 25, 29),
}


def _program_attr_height(port: vdc.VdcPorthole, r9: int) -> int:
    """Apply one row of :data:`ATTR_HEIGHT_TIMING`; returns the attribute row
    count. Written in ascending register order: every intermediate state is out
    of sync for the ~200 us the four writes take, which is far inside one frame,
    so there is no ordering that a monitor can tell apart."""
    v_total, v_displayed, v_sync = ATTR_HEIGHT_TIMING[r9]
    port.write_reg(vdc.R.V_TOTAL, v_total)
    port.write_reg(vdc.R.V_DISPLAYED, v_displayed)
    port.write_reg(vdc.R.V_SYNC_POS, v_sync)
    port.write_reg(vdc.R.CHAR_V_TOTAL, r9)
    return v_displayed


def stage_attr_height(client: TRClient, port: vdc.VdcPorthole, hold: float) -> None:
    """Sweep the attribute block height and show each setting as color bands.

    Needs a person at the RGBI monitor. Whether the VDC really fetches only
    ``80 x rows`` attribute bytes is a question about what reaches the screen,
    and there is no capture on that output. Each pass fills the bitmap solid and
    writes one color per attribute row, so the answer is a band count: 100 bands
    two pixels tall at R9 = 1, 25 bands eight pixels tall at R9 = 7. A wrong
    count, a rolling picture, or color that repeats down the screen all say the
    plane is not the size the arithmetic claims.

    The payoff is not the full-frame rate -- the bitmap is 16000 bytes whatever
    R9 does, so a full frame can never beat ~1.1 fps through the porthole. It is
    the delta path: at R9 = 7 the attribute plane is a quarter the size, so
    attribute deltas cost a quarter as much, which is most of the cost for
    content whose color moves slower than its detail."""
    print("\n[5c] attribute block height (R9) -- NEEDS EYES ON THE RGBI MONITOR")
    solid = bytes([0xFF]) * vdc.BITMAP_BYTES
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, solid)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.BITMAP_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)

    try:
        for r9 in sorted(ATTR_HEIGHT_TIMING):
            rows = _program_attr_height(port, r9)
            nbytes = rows * vdc.ATTR_COLS
            frame = vdc.BITMAP_BYTES + nbytes
            # Colors 1-15: 0 is black against a solid-foreground bitmap, which
            # would read as a missing band rather than a band.
            plane = bytearray()
            for row in range(rows):
                plane += bytes([(row % 15) + 1]) * vdc.ATTR_COLS
            client.write_segment(vdc_rom.FRAMEBUF_ADDR, bytes(plane))
            before = issue(
                client,
                vdc_rom.CMD_BLIT,
                dst=vdc.ATTR_BASE,
                count=nbytes,
                src=vdc_rom.FRAMEBUF_ADDR,
            )
            dt = wait_done(client, before, timeout=30.0)
            print(
                f"    R9={r9}  8x{r9 + 1} blocks  {rows:3d} rows  "
                f"{nbytes:5d} B attrs  frame {frame:5d} B  "
                f"attr blit {dt * 1000:.0f} ms"
            )
            print(f"           -> expect {rows} bands of {r9 + 1} px, cycling 15 colors, no black")
            time.sleep(hold)
    finally:
        _program_attr_height(port, vdc.BITMAP_640x200_REGS[vdc.R.CHAR_V_TOTAL])

    base = vdc.FRAME_BYTES
    for r9 in sorted(ATTR_HEIGHT_TIMING):
        frame = vdc.BITMAP_BYTES + ATTR_HEIGHT_TIMING[r9][1] * vdc.ATTR_COLS
        print(f"    R9={r9}: frame {frame} B, {(1 - frame / base) * 100:4.1f}% off 24000")


def pattern_image(client: TRClient, image_path: str) -> None:
    import cv2

    print(f"\n[6] pattern: {image_path} -> 640x200 8x2-color bitmap")
    img = cv2.imread(image_path)
    if img is None:
        raise SystemExit(f"could not read image {image_path!r}")
    img = cv2.resize(img, (vdc.BITMAP_W, vdc.BITMAP_H), interpolation=cv2.INTER_AREA)
    idx = vdc.quantize_to_vdc(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    bitmap, attr = vdc.pack_bitmap_frame(idx)

    client.write_segment(vdc_rom.FRAMEBUF_ADDR, bitmap + attr)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.FRAME_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)
    residual, wedged = _repair(client, bitmap + attr)
    if not wedged:
        print(f"    repair: {'clean' if not residual else f'{len(residual)} px still wrong'}")
    print("    -> the 80-col screen should show the picture.")
    print("       Compare against scripts/diags/vdc_preview.py for this image.")


#: Sixteen bands of 12 scanlines, which is 192 of the 200 and leaves every band
#: boundary on an even line. That matters: a color block is 8x2, so a band edge
#: on an odd line would put two band colors and the label color in one block,
#: and a block only carries two.
PALETTE_BAND_H: Final = 12

#: Commodore's 80-column names, from the color table in the C128 Programmer's
#: Reference Guide. Deliberately not the VIC-II names: the two tables differ,
#: and entry 12 is "dark yellow" on this screen where the 40-column list says
#: brown.
PALETTE_NAMES: Final = (
    "BLACK",
    "DARK GRAY",
    "DARK BLUE",
    "LIGHT BLUE",
    "DARK GREEN",
    "LIGHT GREEN",
    "DARK CYAN",
    "LIGHT CYAN",
    "DARK RED",
    "LIGHT RED",
    "DARK PURPLE",
    "LIGHT PURPLE",
    "DARK YELLOW",
    "LIGHT YELLOW",
    "LIGHT GRAY",
    "WHITE",
)


def pattern_palette(client: TRClient) -> None:
    """One band per palette entry, each labeled in whichever of black or white
    stands off it further."""
    import cv2
    import numpy as np

    print("\n[6] pattern: the 16-color palette, one labeled band each")
    idx = np.zeros((vdc.BITMAP_H, vdc.BITMAP_W), dtype=np.uint8)
    luma = vdc.VDC_PALETTE @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    for color in range(16):
        y0 = color * PALETTE_BAND_H
        band = idx[y0 : y0 + PALETTE_BAND_H]
        band[:] = color
        ink = 0 if luma[color] > 128 else 15
        # Draw into a scratch mask so putText's antialiasing cannot invent a
        # third color inside a block that can only hold two.
        mask = np.zeros(band.shape, dtype=np.uint8)
        cv2.putText(
            mask,
            f"{color:2d}  {PALETTE_NAMES[color]}",
            (8, PALETTE_BAND_H - 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            255,
            1,
            cv2.LINE_8,
        )
        band[mask > 0] = ink

    bitmap, attr = vdc.pack_bitmap_frame(idx)
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, bitmap + attr)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.FRAME_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)
    residual, wedged = _repair(client, bitmap + attr)
    if not wedged:
        print(f"    repair: {'clean' if not residual else f'{len(residual)} px still wrong'}")
    print("    -> 16 labeled bands, black at the top and white at the bottom.")


def pattern_animate(client: TRClient, seconds: float) -> None:
    """A moving bar, blitting only the attribute plane. Deliberately the cheap
    plane: it is the honest demonstration of what this path can sustain."""
    print(f"\n[6] pattern: scrolling attribute bar for {seconds:.0f}s")
    print("    -> a horizontal color band should sweep DOWN the 80-col screen,")
    print("       smoothly and without tearing or speckle.")
    frames = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        row = frames % vdc.ATTR_ROWS
        plane = bytearray([0x00]) * vdc.ATTR_BYTES
        for r in range(row, min(row + 8, vdc.ATTR_ROWS)):
            plane[r * vdc.ATTR_COLS : (r + 1) * vdc.ATTR_COLS] = (
                bytes([((r % 15) + 1) << 4]) * vdc.ATTR_COLS
            )
        client.write_segment(vdc_rom.FRAMEBUF_ADDR, bytes(plane))
        before = issue(
            client,
            vdc_rom.CMD_BLIT,
            dst=vdc.ATTR_BASE,
            count=vdc.ATTR_BYTES,
            src=vdc_rom.FRAMEBUF_ADDR,
        )
        wait_done(client, before)
        frames += 1
    dt = time.perf_counter() - t0
    print(f"    {frames} frames in {dt:.1f}s  ->  {frames / dt:.1f} fps sustained")


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", help="TR over TCP")
    ap.add_argument("--serial", metavar="PORT", help="TR over serial")
    ap.add_argument("--pattern", choices=("none", "image", "animate", "palette"), default="none")
    ap.add_argument("--image", help="source image for --pattern image")
    ap.add_argument(
        "--hold",
        type=float,
        default=20.0,
        help="seconds to run --pattern animate / hold a still image",
    )
    ap.add_argument(
        "--rate-bytes",
        type=int,
        default=vdc.FRAME_BYTES,
        help="payload size for the DMA and blit rate stages",
    )
    ap.add_argument("--reset-settle", type=float, default=3.0)
    ap.add_argument("--no-dwell", action="store_true", help="skip the VRAM dwell stage")
    ap.add_argument(
        "--no-blank-gain",
        action="store_true",
        help="skip the R1 = 0 display-contention stage",
    )
    ap.add_argument(
        "--attr-height",
        action="store_true",
        help=(
            "sweep the attribute block height (R9 = 1, 3, 7) as color bands. "
            "Scored by counting bands on the RGBI monitor, so only run it "
            "with someone watching."
        ),
    )
    ap.add_argument("--dwell-bytes", type=int, default=6000)
    ap.add_argument(
        "--dwell-secs",
        type=lambda v: tuple(int(x) for x in v.split(",")),
        default=(2, 10, 30, 90),
        help="dwell sample times, comma-separated seconds",
    )
    ap.add_argument(
        "--pattern-only",
        action="store_true",
        help=(
            "skip the rate stages and go straight to --pattern. A blit hangs "
            "roughly one run in six, and a run that needs a person watching the "
            "monitor should risk that once rather than four times."
        ),
    )
    ap.add_argument(
        "--no-reset-exit",
        action="store_true",
        help="leave the cartridge running (it will not stop on its own)",
    )
    args = ap.parse_args()

    if args.pattern == "image" and not args.image:
        ap.error("--pattern image needs --image PATH")

    client = connect(tcp=args.tcp, serial=args.serial)
    try:
        if not stage_launch(client, args.reset_settle):
            return 1
        port = make_porthole(client)
        stage_boot_state(port)

        if not args.pattern_only:
            payload = stage_dma_rate(client, args.rate_bytes)
            stage_blit_rate(client, port, payload)
            if not args.no_dwell:
                stage_vram_dwell(port, min(len(payload), args.dwell_bytes), args.dwell_secs)
            stage_end_to_end(client)
            if not args.no_blank_gain:
                stage_blank_gain(client, port, payload)
        if args.attr_height:
            stage_attr_height(client, port, args.hold)

        if args.pattern == "image":
            pattern_image(client, args.image)
            print(f"\nlook at the RGBI monitor — holding {args.hold:.0f}s ...")
            time.sleep(args.hold)
        elif args.pattern == "animate":
            pattern_animate(client, args.hold)
        elif args.pattern == "palette":
            pattern_palette(client)
            print(f"\nlook at the RGBI monitor — holding {args.hold:.0f}s ...")
            time.sleep(args.hold)
    except (OSError, TRError, TimeoutError) as e:
        print(f"\nABORTED: {e}")
        return 1
    finally:
        if not args.no_reset_exit:
            with contextlib.suppress(OSError, TRError):
                blank_screen(make_porthole(client))
            print("\nresetting the C128 ...")
            with contextlib.suppress(OSError, TRError):
                client.reset()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
