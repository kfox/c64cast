#!/usr/bin/env python3
"""De-risk a HOST-ONLY (no Cam Link) self-calibration of the Ultimate Audio
sampler's true reference clock, via the end-of-sample interrupt.

Why this exists
---------------
The sampler plays PCM out of a REU ring at REF/divider; a unit's real REF can
differ from the firmware-nominal 6.25 MHz (HW-measured ~2% slow on one U64-II),
so sampler audio drifts against the host-clock-paced video. Today the only fix
is a hand-tuned ``[audio].sampler_clock_hz`` (measured with sampler_clock_calib.py,
which needs the Cam Link HDMI-audio rig). We want that number derived
automatically, out of the box.

The FPGA exposes **no read-position register** — but the control register has an
interrupt-on-end-of-sample bit (b2) and a latching IRQ status at ``$DF20``
(cleared via ``$DF3F``). So if we play a **known-length one-shot** and time
``gate -> end-of-sample IRQ`` on the host clock, ``true_rate = N / elapsed``
gives the effective REF with NO capture hardware. This probe answers the
linchpin question before any production change:

  (A) one-shot: does end-of-sample IRQ fire + read back at $DF20, and does the
      timing yield a sane effective REF (~6.12-6.25 MHz on .64)?
  (B) repeat:   in A<->B loop mode with interrupt set, does the IRQ re-fire at
      each ring wrap? (that would enable LIVE refinement during streaming.)

Usage
-----
    scripts/diags/sampler_irq_clock_probe.py                      # default .64, one-shot x3
    scripts/diags/sampler_irq_clock_probe.py --url u64://192.168.2.64
    scripts/diags/sampler_irq_clock_probe.py --mode repeat --seconds 1.0
    scripts/diags/sampler_irq_clock_probe.py --mode both

Reads $DF20 over REST (does NOT contend with the DMA write socket) at a modest
poll rate; keeps runs short. Resets the machine on exit unless --no-reset.
Requires the sampler mapped + REU enabled (auto-provisioned here, restored at exit).
"""

from __future__ import annotations

import argparse
import statistics
import time

import _diaglib as d


def _probe(
    url: str, mode: str, seconds: float, repeats: int, poll_hz: float, no_reset: bool
) -> int:
    import numpy as np

    import c64cast.config as cfgmod
    import c64cast.doctor as doctor
    from c64cast.backend import make_backend
    from c64cast.connect import apply_to_config, parse_connection_uri
    from c64cast.sampler import (
        DEFAULT_RING_BASE,
        SAMPLER_PAN_CENTER,
        SAMPLER_REF_CLOCK,
        SAMPLER_VOLUME_MAX,
        actual_rate_for_divider,
        channel_base,
        channel_register_writes,
        control_byte,
        divider_for_rate,
    )

    cfg = cfgmod.Config()
    apply_to_config(cfg, parse_connection_uri(url))
    cfg.ultimate64.auto_reu = True
    api = make_backend(cfg)

    # REST base for reading $DF20 (address WITHOUT $, per the recurring gotcha).
    rest_url = url.replace("u64://", "http://").split("?", 1)[0]
    if not rest_url.startswith("http"):
        rest_url = d.U64_URL

    cbase = channel_base(0)  # $DF20
    status_addr = cbase  # reads here = IRQ status
    clear_addr = cbase + 0x1F  # $DF3F: write 1=this ch, $FF=all

    # Program at the NOMINAL ref so the divider matches what production uses;
    # the measured elapsed then reveals the REAL ref.
    divider = divider_for_rate(44100, SAMPLER_REF_CLOCK)
    nominal_rate = actual_rate_for_divider(divider, SAMPLER_REF_CLOCK)  # REF/div
    rate_i = int(round(nominal_rate))
    bps = 2  # 16-bit
    ring_base = DEFAULT_RING_BASE

    def clear_irq() -> None:
        api.write_memory(f"{clear_addr:04X}", "FF")
        api.flush()

    def read_status() -> int | None:
        b = d.rest_readmem(status_addr, 1, url=rest_url, timeout=0.5)
        return b[0] if b else None

    def write_pcm(nbytes: int) -> None:
        # A quiet 300 Hz tone (harmless if the mixer is up) of exactly nbytes.
        nsamp = nbytes // bps
        t = np.arange(nsamp) / rate_i
        pcm = (0.2 * np.sin(2 * np.pi * 300 * t) * 32767).astype("<i2").tobytes()
        SLICE = 32 * 1024
        for off in range(0, len(pcm), SLICE):
            api.reu_write(ring_base + off, pcm[off : off + SLICE])
        api.flush()

    def program(length_bytes: int, repeat: bool) -> None:
        for off, vals in channel_register_writes(
            reu_offset=ring_base,
            length=length_bytes,
            divider=divider,
            volume=SAMPLER_VOLUME_MAX,
            pan=SAMPLER_PAN_CENTER,
            repeat=repeat,
            repeat_a=0,
            repeat_b=length_bytes,
        ):
            api.write_regs(f"{cbase + off:04X}", *vals)
        api.flush()

    def gate_on(repeat: bool) -> float:
        ctrl = control_byte(gate=True, repeat=repeat, interrupt=True, bits=16)
        clear_irq()
        base = read_status()
        print(
            f"      pre-gate status=$DF20={base if base is None else f'0x{base:02X}'}  ctrl=0x{ctrl:02X}"
        )
        api.write_memory(f"{cbase:04X}", f"{ctrl:02X}")
        api.flush()
        return time.monotonic()

    def gate_off() -> None:
        api.write_memory(f"{cbase:04X}", "00")
        api.flush()

    reu_restore = doctor.provision_reu(api, cfg)
    samp_restore = doctor.provision_sampler(api, cfg)
    dt = 1.0 / poll_hz
    rc = 1
    try:
        nbytes = (int(seconds * rate_i)) * bps
        expected = nbytes / bps / nominal_rate
        print(
            f"[setup] nominal ref {SAMPLER_REF_CLOCK} Hz, div {divider}, "
            f"rate {rate_i} Hz; one-shot {seconds:.2f}s = {nbytes} B; "
            f"expected end at ~{expected:.3f}s (if REF were nominal)"
        )
        write_pcm(nbytes)

        if mode in ("oneshot", "both"):
            print("\n=== TEST A: one-shot end-of-sample IRQ ===")
            refs: list[float] = []
            for i in range(repeats):
                base = read_status() or 0
                t0 = gate_on(repeat=False)
                fired_at: float | None = None
                deadline = t0 + expected * 1.5 + 2.0
                last = base
                while time.monotonic() < deadline:
                    s = read_status()
                    if s is not None and (s & ~base) != 0:
                        fired_at = time.monotonic()
                        if s != last:
                            print(f"      status changed -> 0x{s:02X} at t={fired_at - t0:.3f}s")
                        break
                    if s is not None and s != last:
                        print(f"      status -> 0x{s:02X} at t={time.monotonic() - t0:.3f}s")
                        last = s
                    time.sleep(dt)
                gate_off()
                if fired_at is None:
                    print(
                        f"  [{i + 1}/{repeats}] NO IRQ within {deadline - t0:.1f}s  <-- mechanism absent?"
                    )
                    clear_irq()
                    time.sleep(0.2)
                    continue
                elapsed = fired_at - t0
                true_rate = (nbytes / bps) / elapsed
                eff_ref = true_rate * divider
                refs.append(eff_ref)
                ratio = true_rate / nominal_rate
                print(
                    f"  [{i + 1}/{repeats}] IRQ at {elapsed:.3f}s  ->  true_rate {true_rate:.1f} Hz "
                    f"({'SLOW' if ratio < 1 else 'FAST'} {abs(ratio - 1) * 100:.2f}%)  "
                    f"eff REF {eff_ref:,.0f} Hz"
                )
                clear_irq()
                time.sleep(0.2)
            if refs:
                med = statistics.median(refs)
                print(
                    f"\n  => median effective REF {med:,.0f} Hz  "
                    f"(nominal {SAMPLER_REF_CLOCK:,}, ear-tuned .64 was 6,155,000)"
                )
                print(f"  => suggested [audio].sampler_clock_hz = {round(med / 1000) * 1000}")
                rc = 0
            else:
                print("\n  => one-shot IRQ mechanism did NOT fire — dead end for host-only cal.")

        if mode in ("repeat", "both"):
            print("\n=== TEST B: repeat-mode wrap IRQ (live-refine feasibility) ===")
            # Small ring so wraps are frequent; reprogram length = a short loop.
            loop_s = min(seconds, 1.0)
            loop_bytes = (int(loop_s * rate_i)) * bps
            program(loop_bytes, repeat=True)
            base = read_status() or 0
            t0 = gate_on(repeat=True)
            wraps: list[float] = []
            last = base
            end = t0 + max(6.0, loop_s * 8)
            while time.monotonic() < end:
                s = read_status()
                if s is not None and (s & ~base) != 0:
                    tw = time.monotonic()
                    wraps.append(tw - t0)
                    print(f"      wrap IRQ #{len(wraps)} at t={tw - t0:.3f}s (status 0x{s:02X})")
                    clear_irq()
                time.sleep(dt)
            gate_off()
            if len(wraps) >= 2:
                periods = [b - a for a, b in zip(wraps, wraps[1:], strict=False)]
                per = statistics.median(periods)
                true_rate = (loop_bytes / bps) / per
                eff_ref = true_rate * divider
                print(
                    f"  => {len(wraps)} wraps, median period {per:.3f}s "
                    f"(expected {loop_bytes / bps / nominal_rate:.3f}s) -> eff REF {eff_ref:,.0f} Hz"
                )
                print("  => repeat-wrap IRQ WORKS: live streaming refinement is feasible.")
                rc = 0
            else:
                print(f"  => only {len(wraps)} wrap IRQ(s) seen — repeat-mode wrap IRQ not usable.")
    finally:
        gate_off()
        clear_irq()
        doctor.restore_sampler(api, samp_restore)
        del reu_restore
        if not no_reset:
            d.rest_reset(rest_url)
            print("\n[teardown] machine reset.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="u64://192.168.2.64", help="connection URI (u64://HOST)")
    ap.add_argument("--mode", choices=("oneshot", "repeat", "both"), default="oneshot")
    ap.add_argument("--seconds", type=float, default=8.0, help="one-shot sample length (s)")
    ap.add_argument("--repeats", type=int, default=3, help="one-shot measurements to median")
    ap.add_argument("--poll-hz", type=float, default=40.0, help="$DF20 poll rate")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()
    return _probe(a.url, a.mode, a.seconds, a.repeats, a.poll_hz, a.no_reset)


if __name__ == "__main__":
    raise SystemExit(main())
