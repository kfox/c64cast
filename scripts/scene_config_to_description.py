#!/usr/bin/env python3
"""Turn a c64cast log file into a pasteable YouTube description blob.

`c64cast` logs one SCENE_CONFIG_JSON line per scene activation (see
c64cast/recording_metadata.py) — a snapshot of that scene's coalesced
settings (display mode, color/audio config, hardware, source/copyright info).
This script extracts those lines from a `--log-file` run and renders a
human, paste-ready text block.

Run via `python scripts/scene_config_to_description.py LOGFILE` (or
`uv run python scripts/...` — needs `c64cast` importable, same as
scripts/bench.py).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from c64cast.recording_metadata import extract_scene_configs, render_description  # noqa: E402

_RULE = "=" * 40


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file", help="c64cast --log-file output to read")
    parser.add_argument(
        "--all", action="store_true", help="render every scene_config entry found, in order"
    )
    parser.add_argument(
        "--index", type=int, default=None, help="render one specific entry (0-based)"
    )
    parser.add_argument("-o", "--output", help="write to this path instead of stdout")
    args = parser.parse_args(argv)

    try:
        with open(args.log_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"error: could not read {args.log_file!r}: {e}", file=sys.stderr)
        return 1

    entries = extract_scene_configs(text)
    if not entries:
        print(f"error: no SCENE_CONFIG_JSON entries found in {args.log_file!r}", file=sys.stderr)
        return 1

    if args.index is not None:
        if not (0 <= args.index < len(entries)):
            print(
                f"error: --index {args.index} out of range (found {len(entries)} entries)",
                file=sys.stderr,
            )
            return 1
        selected = [entries[args.index]]
    elif args.all:
        selected = entries
    else:
        selected = [entries[-1]]

    blocks = [render_description(e) for e in selected]
    output = f"\n{_RULE}\n".join(blocks) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
