#!/usr/bin/env python3
"""Drive the TeensyROM+ backend's audio paths on real hardware and capture the
result off the Cam Link (the TR's C64 HDMI carries SID out), so "I heard audio"
becomes a measured level instead of a guess.

This exercises the SAME c64cast code the playlist uses — the real
`TeensyROMBackend` write path + `AudioStreamer` (host-DMA NMI DAC) for tone
mode, and `backend.run_sid_player` for SID mode — not a reimplementation, so a
pass here means the production path works on the TR.

Two modes:

  * ``--mode tone`` (default) — host-DMA NMI DAC: bring up the IRQ clear-loop
    idle, then stream a sine tone via ``AudioStreamer.start_for_external_source``
    + ``push_samples`` (the same path live mic / video audio take). Confirms the
    NMI vector install + CIA #2 NMI actually fire on the TR's C64.
  * ``--mode sid --sid PATH`` — ``run_sid_player``: DMA the SID payload + player
    MC + re-INIT stub, then a pure-DMA ``$0314`` vector-swap to the re-INIT stub
    (over the running IRQ-enabled clear-loop — no reset/boot/LaunchFile, the same
    mechanism as subtune cycling). This is the WaveformScene audio path; confirms
    the SID plays on the real chip.

While playing, a ffmpeg/avfoundation capture records the Cam Link audio; the
tool then reports ffmpeg ``volumedetect`` mean/max dB (a silent capture ~ −91 dB
floor means no audio reached the SID). Optionally flashes the $D020 border as a
1 Hz A/V sync marker (--flash) per the border-flash technique.

    scripts/diags/tr_audio_sid_probe.py --tcp 192.168.2.164
    scripts/diags/tr_audio_sid_probe.py --serial /dev/cu.usbmodem* --mode tone --freq 440
    scripts/diags/tr_audio_sid_probe.py --tcp 192.168.2.164 --mode sid --sid assets/sids/x.sid

Stop gracefully — this tool silences the SID + resets the C64 on the way out
(the standing silence-and-reset rule) unless --no-reset-exit. Never blank the
display on the TR (DEN-off hangs the cycle-clean DMA); this tool never does.
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from dataclasses import replace

import _diaglib as d
import numpy as np

from c64cast.audio import AudioStreamer
from c64cast.backend import TEENSYROM_PROFILE
from c64cast.teensyrom_api import TeensyROMBackend
from c64cast.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
)

SAMPLE_RATE = 10_500  # c64cast's default NMI DAC consumer rate
BORDER_ADDR = 0xD020


def build_backend(*, tcp_host: str | None, serial_port: str | None) -> TeensyROMBackend:
    if serial_port:
        transport = SerialTransport(serial_port, DEFAULT_BAUD)
        kind = "tr_serial"
    elif tcp_host:
        transport = TcpTransport(tcp_host, DEFAULT_TCP_PORT)
        kind = "tr_tcp"
    else:
        raise SystemExit("need --tcp HOST or --serial PORT")
    profile = replace(TEENSYROM_PROFILE, write_transport=kind)
    return TeensyROMBackend(transport, profile=profile, storage="sd")


def bring_up(api: TeensyROMBackend) -> None:
    """Mirror cli.py's TR bring-up: reset -> IRQ clear-loop idle -> case-switch
    off. Leaves the kernal IRQ running so NMI + SID-player IRQ have a live CPU."""
    print("bring-up: reset + run BASIC clear loop")
    api.reset()
    time.sleep(1.0)
    api.run_basic_clear_loop()
    api.disable_case_switch()


def record_camlink(device: str, seconds: float, out_path: str) -> subprocess.Popen:
    """Start a non-blocking ffmpeg avfoundation capture of the Cam Link audio.
    Start this BEFORE the playback so the boot/first-tick window is covered."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-i", device,
        "-t", str(seconds), "-ac", "1", "-ar", "48000", out_path,
    ]  # fmt: skip
    print(f"capture: {seconds:g}s from avfoundation {device} -> {out_path}")
    return subprocess.Popen(cmd)


def analyze(path: str) -> None:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )  # fmt: skip
    for line in r.stderr.splitlines():
        if "mean_volume" in line or "max_volume" in line:
            print("  " + line.strip().split("] ", 1)[-1])


def _flasher(api: TeensyROMBackend, stop: threading.Event) -> None:
    """1 Hz $D020 border flash as an A/V sync marker until `stop` is set."""
    on = False
    while not stop.wait(0.5):
        on = not on
        try:
            api.write_memory(f"{BORDER_ADDR:04X}", "01" if on else "00")
        except Exception:
            return


def play_tone(api: TeensyROMBackend, seconds: float, freq: float) -> None:
    """Stream a sine tone through the host-DMA NMI DAC (the live-mic path)."""
    audio = AudioStreamer(api, SAMPLE_RATE, "NTSC", use_reu_pump=False)
    audio.start_for_external_source()
    try:
        chunk = SAMPLE_RATE // 10  # 100 ms blocks
        total = int(seconds * SAMPLE_RATE)
        phase = 0.0
        step = 2 * np.pi * freq / SAMPLE_RATE
        pushed = 0
        while pushed < total:
            n = min(chunk, total - pushed)
            idx = np.arange(n)
            samples = (0.6 * np.sin(phase + step * idx) * 32767).astype(np.int16)
            phase = (phase + step * n) % (2 * np.pi)
            audio.push_samples(samples)
            pushed += n
            time.sleep(n / SAMPLE_RATE)
        time.sleep(1.0)  # let the C64 ring drain
    finally:
        audio.stop()


def play_sid(api: TeensyROMBackend, sid_path: str, seconds: float) -> None:
    """Play a SID file via run_sid_player (the WaveformScene audio path)."""
    with open(sid_path, "rb") as fh:
        sid_bytes = fh.read()
    print(f"run_sid_player: {sid_path} ({len(sid_bytes)} bytes)")
    api.run_sid_player(sid_bytes)
    time.sleep(seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tcp", metavar="HOST", help="TR TCP listener host")
    ap.add_argument("--serial", metavar="PORT", help="TR serial port (/dev/cu.usbmodem*)")
    ap.add_argument("--mode", choices=("tone", "sid"), default="tone")
    ap.add_argument("--sid", help="SID file for --mode sid")
    ap.add_argument("--freq", type=float, default=440.0, help="tone frequency (Hz)")
    ap.add_argument("--secs", type=float, default=8.0, help="playback seconds")
    ap.add_argument("--device", default=d.CAMLINK_AVF_AUDIO, help="avfoundation audio device")
    ap.add_argument("--flash", action="store_true", help="1 Hz $D020 border sync marker")
    ap.add_argument("--no-capture", action="store_true", help="skip Cam Link capture")
    ap.add_argument("--no-reset-exit", action="store_true", help="leave the C64 running")
    args = ap.parse_args()

    if args.mode == "sid" and not args.sid:
        ap.error("--mode sid needs --sid PATH")

    api = build_backend(tcp_host=args.tcp, serial_port=args.serial)
    cap: subprocess.Popen | None = None
    stop_flash = threading.Event()
    flash_thread: threading.Thread | None = None
    try:
        bring_up(api)
        # Start the capture FIRST (covers any bring-up/first-tick latency).
        if not args.no_capture:
            out_path = str(d.stamped(f"tr_{args.mode}", "wav"))
            cap = record_camlink(args.device, args.secs + 3.0, out_path)
            time.sleep(0.5)  # let the recorder come up
        if args.flash:
            flash_thread = threading.Thread(target=_flasher, args=(api, stop_flash), daemon=True)
            flash_thread.start()

        if args.mode == "tone":
            print(f"playing {args.secs:g}s tone @ {args.freq:g} Hz via NMI DAC")
            play_tone(api, args.secs, args.freq)
        else:
            play_sid(api, args.sid, args.secs)
    finally:
        stop_flash.set()
        if flash_thread is not None:
            flash_thread.join(timeout=1.0)
        if cap is not None:
            cap.wait()
            print("volumedetect:")
            analyze(out_path)
        try:
            api.silence_sid()
            api.flush()
            if not args.no_reset_exit:
                api.reset()
        finally:
            api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
