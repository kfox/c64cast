"""Finding and probing the audio-capture input for ``--calibrate-dac``.

The measurement needs the input the C64's audio actually arrives on — almost
always an HDMI capture device — and the penalty for guessing wrong is
expensive: the system default input is usually the on-board microphone, which
records room noise for ~50 s and measures like a dead chip. So device
selection (:func:`find_capture_device` + the name hints), format probing
(:func:`resolve_capture_format`), and the failure text that names the device
recorded from and lists the alternatives (:func:`capture_fault_message`) live
together here. sounddevice is imported inside each function, so importing
this module costs nothing when the ``mic`` extra is absent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from .dac_slot_ring import CAP_SR

log = logging.getLogger(__name__)

#: Rates to fall back to, in order, when a capture device won't do `CAP_SR`.
#: The cheap MacroSilicon-based HDMI→USB dongles are frequently 96 kHz-only,
#: and some UVC inputs only offer 44.1 kHz. Every consumer of a capture takes
#: its rate as a parameter, so any of these measures correctly.
CAP_SR_FALLBACKS = (96000, 44100, 32000)

#: Name fragments that identify an input as video-capture hardware, most
#: specific first. The measurement needs the input the C64's audio arrives on,
#: which is essentially always an HDMI capture device — but only the author's
#: Cam Link used to be recognized, so every other rig silently fell through to
#: the *system default input*. On Windows that is the on-board microphone, which
#: records room noise and measures like a dead chip (see
#: :data:`RING_SPREAD_NOT_THE_RING`). These cover the common sticks: Elgato, the
#: MacroSilicon-based HDMI→USB dongles ("USB Video", "USB3.0 HD Video Capture"),
#: and anything self-describing as a capture/HDMI input.
CAPTURE_NAME_HINTS = (
    "cam link",
    "elgato",
    "hdmi",
    "capture",
    "macrosilicon",
    "usb video",
    "av to usb",
)


def looks_like_capture_input(name: str) -> bool:
    """Whether an input device's name identifies it as video-capture hardware.
    Used to pick one automatically, and to warn when the fallback lands on
    something that is probably a microphone."""
    low = name.lower()
    return any(h in low for h in CAPTURE_NAME_HINTS)


def find_capture_device(preferred: int | None) -> int:
    """Resolve the capture device index: `preferred` if given, else the first
    input-capable device whose name looks like video-capture hardware
    (:data:`CAPTURE_NAME_HINTS`, in order), else the system default input.

    The hints are tried in order rather than scanning devices once, so a rig
    with both a Cam Link and some other HDMI input still picks the Cam Link."""
    import sounddevice as sd

    if preferred is not None:
        return preferred
    devices = list(sd.query_devices())
    for hint in CAPTURE_NAME_HINTS:
        for i, dev in enumerate(devices):
            if hint in str(dev["name"]).lower() and dev["max_input_channels"] > 0:
                return i
    default_in = sd.default.device[0]
    return int(default_in) if default_in is not None and default_in >= 0 else 0


class CaptureUnavailableError(RuntimeError):
    """Raised when sounddevice / a usable capture device isn't available."""


def _input_device_list() -> str:
    """One-line-per-device listing of every input-capable device, for error text."""
    import sounddevice as sd

    lines = [
        f"  {i}: {d['name']} ({d['max_input_channels']} in)"
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
    return "\n".join(lines) or "  (none)"


def pick_device_hint(lead: str = "Pick one with") -> str:
    """The "and here are your inputs" footer every capture-device failure ends
    with. ``lead`` carries the sentence into it, so the call sites differ only
    in their verb instead of each restating the flag and the listing."""
    return f"{lead} --audio-device N:\n{_input_device_list()}"


def capture_fault_message(dev: int, reason: str, peak: float, saved: Path | None = None) -> str:
    """The message a capture that doesn't contain the slot ring fails with.

    Everything upstream of this can only say *what* it saw — "found 1 ring sync
    marker", "the passes disagree by 100 %" — and that reads like a bug in the
    measurement when it is almost always the rig. So the failure names the
    device it recorded from, states how loud that recording was, and lists the
    inputs to pick from instead."""
    import sounddevice as sd

    try:
        name = str(sd.query_devices(dev)["name"])
    except Exception:  # noqa: BLE001 — the name is decoration; the advice isn't
        name = "?"
    return (
        f"capture device {dev} ({name!r}) is not carrying the calibration ring "
        f"(peak {peak:.5f} of full scale): {reason}.\nLikely causes, in order:\n"
        "  • it is the wrong input. An on-board microphone records room noise, "
        "which measures exactly like this — the capture has to be the input the "
        "C64's audio actually arrives on (HDMI capture stick, Cam Link, or a "
        "line-in fed from the AV port).\n"
        "  • the C64's audio isn't reaching it — HDMI audio off, the cable in "
        "the wrong jack, or the input's gain at zero.\n"
        "  • the NMI DAC never came up on the C64. Re-run with -v and check the "
        "bring-up lines.\n"
        + pick_device_hint("Pick the input with")
        + (f"\nThe capture is saved at {saved}." if saved is not None else "")
    )


class CaptureFormat(NamedTuple):
    """A channel count + sample rate the capture device actually accepts."""

    channels: int
    samplerate: int


def resolve_capture_format(dev: int) -> CaptureFormat:
    """Probe `dev` for a workable (channels, samplerate), preferring stereo at
    :data:`CAP_SR` and widening from there.

    Capture hardware is not all Cam Link. A mono-only UVC input rejects
    ``channels=2`` with PortAudio's generic -9998 "Invalid number of channels",
    and the cheap MacroSilicon-based HDMI→USB dongles are commonly 96 kHz-only
    — either of which used to abort a calibration run with a raw
    ``PortAudioError`` traceback, because the capture was hardcoded to stereo at
    48 kHz. Neither restriction actually prevents a measurement: the levels are
    read off one folded-to-mono channel, and every step of
    :func:`extract_slot_levels` derives its timing from the rate it is handed.

    Rate is the outer loop — a 48 kHz mono capture beats a 96 kHz stereo one,
    since the fallback rates are the compromise and the channel fold is free.
    The device's own ``default_samplerate`` is tried right after `CAP_SR`, ahead
    of the static fallbacks, so an unusual device still gets its native rate.
    ``check_input_settings`` probes without opening a stream, so a rejected
    combination costs nothing. Mirrors ``AudioStreamer._open_input_stream``'s
    channel fallback for the mic path.
    """
    import sounddevice as sd

    try:
        info = sd.query_devices(dev)
        max_in = int(info["max_input_channels"])
    except Exception as e:  # noqa: BLE001 — bad index / device vanished
        raise CaptureUnavailableError(
            f"capture device {dev} could not be queried: {e}\n" + pick_device_hint()
        ) from e
    name = info["name"]
    if max_in <= 0:
        raise CaptureUnavailableError(
            f"capture device {dev} ({name!r}) has no input channels. " + pick_device_hint()
        )

    channel_options: list[int] = []
    for ch in (2, max_in, 1):
        if 1 <= ch <= max_in and ch not in channel_options:
            channel_options.append(ch)

    rate_options: list[int] = [CAP_SR]
    for sr in (int(info.get("default_samplerate") or 0), *CAP_SR_FALLBACKS):
        if sr > 0 and sr not in rate_options:
            rate_options.append(sr)

    for sr in rate_options:
        for ch in channel_options:
            try:
                sd.check_input_settings(device=dev, channels=ch, samplerate=sr, dtype="float32")
                return CaptureFormat(ch, sr)
            except Exception:  # noqa: BLE001 — unsupported combination; try the next
                log.debug("calib: device %d rejected channels=%d sr=%d", dev, ch, sr, exc_info=True)
    raise CaptureUnavailableError(
        f"capture device {dev} ({name!r}) accepted no combination of channels "
        f"{channel_options} × rates {rate_options}. " + pick_device_hint("Pick another with")
    )
