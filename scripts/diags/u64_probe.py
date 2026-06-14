#!/usr/bin/env python3
"""U64 connectivity + reset probe.

The recurring "is the machine reachable / put it back to a clean state" tool.
Checks REST reachability, the Ultimate DMA Service socket (port 64), and
optionally resets the machine.

    scripts/diags/u64_probe.py                 # check the default U64
    scripts/diags/u64_probe.py --reset         # check, then reset
    scripts/diags/u64_probe.py --url http://192.168.2.65   # the U2+
    scripts/diags/u64_probe.py --reset-only    # just PUT machine:reset

Exit code is 0 only when both REST and DMA service are up.
"""

from __future__ import annotations

import argparse
import sys

import _diaglib as d


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help=f"U64/U2+ base URL (default {d.U64_URL})")
    ap.add_argument(
        "--reset", action="store_true", help="reset the machine after the connectivity checks"
    )
    ap.add_argument(
        "--reset-only", action="store_true", help="skip the checks; just reset and exit"
    )
    args = ap.parse_args()

    if args.reset_only:
        code = d.rest_reset(args.url)
        print(f"reset {args.url}: {'HTTP ' + str(code) if code else 'FAILED'}")
        return 0 if code else 1

    rest = d.rest_ping(args.url)
    dma = d.dma_service_up(args.url)
    print(f"REST {args.url}/        : {'HTTP ' + str(rest) if rest else 'UNREACHABLE'}")
    print(
        f"DMA service (:64)      : {'up' if dma else 'DOWN (enable F2 -> Network Settings -> Ultimate DMA Service)'}"
    )

    if args.reset:
        code = d.rest_reset(args.url)
        print(f"reset                  : {'HTTP ' + str(code) if code else 'FAILED'}")

    return 0 if (rest and dma) else 1


if __name__ == "__main__":
    sys.exit(main())
