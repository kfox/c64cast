#!/usr/bin/env python
"""Explain TeensyROM USB-serial auto-detection on this host.

`autodetect_serial_port()` returns a bare `None` for three unrelated reasons —
pyserial missing, `comports()` raising, or no port matching — which makes a
failed auto-detect impossible to diagnose from the return value alone. This tool
runs the *same* enumeration and prints, per port, every field the matcher reads
plus its accept/reject decision, so a silent `None` becomes a specific cause.

Cross-platform (macOS/Linux/Windows). No c64cast hardware needed — it only
touches the local USB serial bus.

    python scripts/diags/tr_serial_probe.py
    # or, to be sure you hit the project env that has pyserial:
    uv run python scripts/diags/tr_serial_probe.py
"""

from __future__ import annotations

import sys

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)


def main() -> int:
    print(f"python      : {sys.executable}")
    print(f"platform    : {sys.platform}")

    try:
        import serial
        from serial.tools import list_ports
    except ImportError as e:
        print(f"pyserial    : NOT IMPORTABLE ({e})")
        print("\n=> This is why auto-detect returns None. Install the 'tr' extra")
        print("   into the interpreter above (uv sync --extra tr), or run this")
        print("   via `uv run python ...` so it uses the project .venv.")
        return 1
    print(f"pyserial    : {getattr(serial, '__version__', '?')}")

    from c64cast.teensyrom_dma import (
        _TEENSY_USB_VID,
        _TEENSYROM_USB_PID,
        _is_teensyrom_port,
        autodetect_serial_port,
    )

    print(
        f"looking for : VID={_TEENSY_USB_VID:#06x} PID={_TEENSYROM_USB_PID:#06x} "
        f'(or product/description containing "teensyrom")\n'
    )

    ports = list(list_ports.comports())
    if not ports:
        print("comports()  : returned NO ports at all.")
        print("\n=> The OS is not exposing any serial device. Check the USB data")
        print("   cable (not charge-only), the driver, and that the board is on.")
        return 1

    print(f"comports()  : {len(ports)} port(s)\n")
    for p in ports:
        vid = f"{p.vid:#06x}" if p.vid is not None else "None"
        pid = f"{p.pid:#06x}" if p.pid is not None else "None"
        decision = "MATCH" if _is_teensyrom_port(p) else "no"
        print(f"  [{decision:>5}] {p.device}")
        print(f"          vid={vid} pid={pid} serial={p.serial_number!r}")
        print(f"          product={p.product!r} description={p.description!r}")
        print(f"          hwid={p.hwid!r}")

    result = autodetect_serial_port()
    print(f"\nautodetect_serial_port() => {result!r}")
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
