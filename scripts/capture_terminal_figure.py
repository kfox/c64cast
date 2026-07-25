#!/usr/bin/env python3
"""Render a real terminal session to a PNG — the User's Guide's terminal figures.

The companion to [capture_guide_figure.py](capture_guide_figure.py), which
handles the figures that come off the C64 itself. Screenshotting a Terminal
window would drag in whatever else is on the desktop, so instead the command
runs on a pty at a fixed size, its output goes through a terminal emulator
(pyte), and the resulting screen is drawn with the book's mono face. What lands
in the PNG is the genuine output, interactive painting included.

Needs two packages that aren't project dependencies, so run it with them:

    uv run --with pyte --with pillow python scripts/capture_terminal_figure.py ...

Look at the screen as text before rendering it — most of the work is finding
the right moment to stop:

    ... --dump --cols 118 -- .venv/bin/python -m c64cast --doctor --skip-probe

Then render, driving any prompts with timed keystrokes (\\r is RETURN,
\\x1b[B is DOWN). --key may be repeated; the delay is seconds from launch:

    ... -o docs/guide/img/fig-2-2-wizard.png --cols 118 --settle 30 \\
        --key '3.0:\\x1b[B' --key '3.6:\\r' -- .venv/bin/python -m c64cast --init out.toml

Two things that bite:

**Answer the cursor probe.** prompt_toolkit asks the terminal where the cursor
is (DSR-6) and prints a "your terminal doesn't support CPR" banner if nothing
answers. pyte models the screen but never replies, so the driver does.

**--sub rewrites before layout, not after.** Substituting a home directory for
a neutral one changes the string's length, so it has to happen upstream of the
column wrap. That means buffering all output, which only works for
non-interactive commands.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pty
import select
import signal
import time
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

MONO = Path.home() / "Library/Fonts/Inconsolata[wdth,wght].ttf"

BG = (0x1B, 0x1D, 0x21)
FG = (0xE4, 0xE6, 0xEA)
CHROME = (0x2C, 0x2F, 0x35)

# pyte names -> RGB. Terminal-ish, tuned to sit calmly next to the book's blue.
ANSI = {
    "black": (0x3B, 0x3F, 0x46),
    "red": (0xE0, 0x6C, 0x75),
    "green": (0x98, 0xC3, 0x79),
    "brown": (0xE5, 0xC0, 0x7B),
    "yellow": (0xE5, 0xC0, 0x7B),
    "blue": (0x61, 0xAF, 0xEF),
    "magenta": (0xC6, 0x78, 0xDD),
    "cyan": (0x56, 0xB6, 0xC2),
    "white": FG,
    "brightblack": (0x6B, 0x71, 0x7B),
    "brightred": (0xFF, 0x8B, 0x94),
    "brightgreen": (0xB5, 0xE8, 0x90),
    "brightyellow": (0xFF, 0xE0, 0x94),
    "brightblue": (0x8A, 0xC6, 0xFF),
    "brightmagenta": (0xE0, 0x9C, 0xFF),
    "brightcyan": (0x7A, 0xDA, 0xE5),
    "brightwhite": (0xFF, 0xFF, 0xFF),
}


def colour(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if name in ("default", ""):
        return default
    if name in ANSI:
        return ANSI[name]
    if len(name) == 6:  # pyte hands back bare hex for 24-bit colour
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
        except ValueError:
            pass
    return default


def run(
    argv: list[str],
    cols: int,
    rows: int,
    keys: list[tuple[float, str]],
    settle: float,
    subs: list[tuple[str, str]] | None = None,
):
    """Drive argv on a pty, optionally typing `keys`, and return the pyte screen.

    With `subs`, output is buffered and rewritten before it reaches the
    emulator rather than after: a replacement that changes length has to happen
    upstream of the 80-column wrap, or the substituted text lands in the wrong
    cells. Buffering means subs only suit non-interactive commands.
    """
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    buffered = bytearray() if subs else None
    env = dict(
        os.environ, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows), FORCE_COLOR="1"
    )

    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ.update(env)
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    t0 = time.monotonic()
    pending = list(keys)
    deadline = t0 + settle + (max((d for d, _ in keys), default=0.0))
    try:
        while True:
            now = time.monotonic()
            while pending and now - t0 >= pending[0][0]:
                os.write(fd, pending.pop(0)[1].encode())
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                if buffered is not None:
                    buffered += data
                else:
                    stream.feed(data)
                # prompt_toolkit probes for the cursor with DSR-6 and warns if
                # nothing answers. pyte models the screen but doesn't reply, so
                # the driver has to — otherwise the figure carries a "your
                # terminal doesn't support CPR" banner that no real one shows.
                if b"\x1b[6n" in data:
                    os.write(
                        fd,
                        f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}R".encode(),
                    )
            if not pending and now > deadline:
                break
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        os.close(fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
    if buffered is not None:
        out = bytes(buffered)
        for old, new in subs or ():
            out = out.replace(old.encode(), new.encode())
        stream.feed(out)
    return screen


def crop_blank(screen, rows: int) -> int:
    """Last non-blank row, so the image isn't mostly empty terminal."""
    last = 0
    for y in range(rows):
        if screen.display[y].strip():
            last = y
    return last + 1


def render(screen, rows_used: int, cols: int, out: Path, size: int = 26) -> None:
    font = ImageFont.truetype(str(MONO), size)
    probe = Image.new("RGB", (10, 10))
    d0 = ImageDraw.Draw(probe)
    cw = d0.textlength("M", font=font)
    ch = round(size * 1.42)

    pad = round(cw * 2)
    bar = round(ch * 1.5)
    w = round(cw * cols) + pad * 2
    h = ch * rows_used + pad * 2 + bar

    img = Image.new("RGB", (w, h), BG)
    draw = ImageDraw.Draw(img)
    # A minimal window bar: enough to read as a terminal, no OS branding.
    draw.rectangle([0, 0, w, bar], fill=CHROME)
    r = round(bar * 0.17)
    for i, dot in enumerate(((0xE0, 0x6C, 0x75), (0xE5, 0xC0, 0x7B), (0x98, 0xC3, 0x79))):
        cx = pad + r + i * (r * 3.2)
        cy = bar / 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dot)

    for y in range(rows_used):
        line = screen.buffer[y]
        for x in range(cols):
            cell = line[x]
            if not cell.data or cell.data == " ":
                if cell.reverse or cell.bg != "default":
                    bgc = colour(cell.fg if cell.reverse else cell.bg, BG)
                    px, py = pad + x * cw, bar + pad + y * ch
                    draw.rectangle([px, py, px + cw, py + ch], fill=bgc)
                continue
            fg = colour(cell.fg, FG)
            bg = colour(cell.bg, BG)
            if cell.reverse:
                fg, bg = bg, fg
            px, py = pad + x * cw, bar + pad + y * ch
            if bg != BG:
                draw.rectangle([px, py, px + cw, py + ch], fill=bg)
            draw.text((px, py), cell.data, font=font, fill=fg)

    img.save(out)
    print(f"{out}  {img.width}x{img.height}  ({rows_used} rows)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out")
    ap.add_argument("--cols", type=int, default=92)
    ap.add_argument("--rows", type=int, default=40)
    ap.add_argument("--settle", type=float, default=3.0, help="seconds to keep reading")
    ap.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="DELAY:TEXT",
        help="type TEXT at DELAY seconds (repeatable); \\r = RETURN, \\x1b[B = DOWN",
    )
    ap.add_argument("--dump", action="store_true", help="print the screen as text instead")
    ap.add_argument(
        "--sub",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rewrite OLD to NEW in the output before it is laid out "
        "(non-interactive commands only; buffers all output)",
    )
    ap.add_argument("--rows-used", type=int, default=0, help="force the rendered row count")
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    if not argv:
        ap.error("give a command after --")

    keys = []
    for k in args.key:
        delay, _, text = k.partition(":")
        keys.append((float(delay), text.encode().decode("unicode_escape")))
    keys.sort()

    subs = [(s.split("=", 1)[0], s.split("=", 1)[1]) for s in args.sub]
    screen = run(argv, args.cols, args.rows, keys, args.settle, subs)
    if args.dump:
        for i, line in enumerate(screen.display):
            print(f"{i:3d}|{line.rstrip()}")
        return 0
    rows_used = args.rows_used or crop_blank(screen, args.rows)
    render(screen, rows_used, args.cols, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
