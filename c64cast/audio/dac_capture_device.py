"""Finding and probing the audio-capture input for ``--calibrate-dac``:
device selection (:func:`find_capture_device`, by name or by the HDMI capture
device's name, never the system default input), format probing
(:func:`resolve_capture_format`), and the failure text that names the device
recorded from and lists the alternatives (:func:`capture_fault_message`).

sounddevice is imported inside each function, so importing this module costs
nothing when the ``mic`` extra is absent.

See docs/architecture/audio.md#picking-the-capture-device.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from .dac_slot_ring import CAP_SR

log = logging.getLogger(__name__)

#: Rates to fall back to, in order, when a capture device won't do `CAP_SR`.
#: The cheap MacroSilicon-based HDMI→USB dongles are frequently 96 kHz-only,
#: and some UVC inputs only offer 44.1 kHz. Every consumer of a capture takes
#: its rate as a parameter, so any of these measures correctly.
CAP_SR_FALLBACKS = (96000, 44100, 32000)


class CaptureUnavailableError(RuntimeError):
    """Raised when sounddevice / a usable capture device isn't available."""


def names_match(a: str, b: str) -> bool:
    """Whether one device name contains the other, ignoring case and outer
    whitespace: how an audio input is matched to the video device it belongs
    to, since a capture stick enumerates both under its own name."""
    a, b = a.strip().lower(), b.strip().lower()
    return bool(a and b) and (a in b or b in a)


def named_positions(names: Sequence[str], spec: str) -> list[int]:
    """The positions in ``names`` that ``spec`` names: the names equal to it,
    ignoring case, else the names that contain it."""
    text = spec.strip().lower()
    exact = [i for i, n in enumerate(names) if n.strip().lower() == text]
    return exact or [i for i, n in enumerate(names) if text and text in n.lower()]


def _input_devices() -> list[tuple[int, str]]:
    """``(index, name)`` of every input-capable sounddevice device."""
    import sounddevice as sd

    return [
        (i, str(d["name"])) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0
    ]


def find_capture_device(preferred: int | str | None) -> int:
    """The sounddevice index of the input the C64's audio arrives on.

    ``preferred`` (``--audio-device``) names it: an index, or a name, matched
    by :func:`named_positions`. With nothing named, it is the one input whose
    name :func:`names_match` the HDMI capture device's that
    :func:`c64cast.control.camera.pick_capture_camera` singles out.

    Raises :class:`CaptureUnavailableError`, listing the inputs, when a name
    matches none or several inputs, when no single capture device is found,
    or when the ``camera`` extra that finds it is missing. The system default
    input is never used in its place: on most machines it is a microphone."""
    inputs = _input_devices()
    if preferred is not None:
        return _named_input(inputs, preferred)
    return _input_named_like_capture_camera(inputs)


def _named_input(inputs: list[tuple[int, str]], preferred: int | str) -> int:
    """The input ``preferred`` names, as :func:`find_capture_device` describes."""
    text = str(preferred).strip()
    try:
        index = int(text)
    except ValueError:
        pass
    else:
        if index < 0:
            raise CaptureUnavailableError(
                f"--audio-device {text} asks for the system default input, which "
                "calibration never records from. " + pick_device_hint("Name the input with")
            )
        return index
    found = named_positions([name for _, name in inputs], text)
    if len(found) == 1:
        return inputs[found[0]][0]
    reason = "matches no audio input" if not found else "matches more than one audio input"
    raise CaptureUnavailableError(
        f"--audio-device {text!r} {reason}. " + pick_device_hint("Name the input with")
    )


def _input_named_like_capture_camera(inputs: list[tuple[int, str]]) -> int:
    """The one input named like the auto-picked HDMI capture device."""
    from c64cast.control import camera

    try:
        cam = camera.pick_capture_camera()
    except camera.CaptureCameraError as e:
        if e.extra_missing:
            raise CaptureUnavailableError(
                f"{e}. Without it the capture input cannot be found, and calibration "
                "does not record from the system default input in its place. Install "
                "the extra, or " + pick_device_hint("name the input with")
            ) from None
        raise CaptureUnavailableError(
            f"{e.reason}, so the capture input, the audio input named like it, cannot "
            f"be found and nothing is recorded. Cameras found:\n"
            f"{camera.camera_listing(e.cameras)}\n" + pick_device_hint("Name the input with")
        ) from None
    found = [i for i, name in inputs if names_match(name, cam.name)]
    if len(found) == 1:
        log.info("calib: capture input %d is named like the capture device %r", found[0], cam.name)
        return found[0]
    reason = "no audio input" if not found else f"{len(found)} audio inputs"
    raise CaptureUnavailableError(
        f"{reason} named like the capture device {cam.name!r}, so nothing is recorded. "
        + pick_device_hint("Name the input with")
    )


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
    with; ``lead`` carries the calling sentence into it."""
    return f"{lead} --audio-device N:\n{_input_device_list()}"


def capture_fault_message(dev: int, reason: str, peak: float, saved: Path | None = None) -> str:
    """The message a capture that doesn't contain the slot ring fails with:
    the device it recorded from, how loud that recording was, the likely
    causes in order, and the inputs to pick from instead."""
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

    Rate is the outer loop — a 48 kHz mono capture beats a 96 kHz stereo one.
    The device's own ``default_samplerate`` is tried right after `CAP_SR`,
    ahead of :data:`CAP_SR_FALLBACKS`. Mirrors
    ``AudioStreamer._open_input_stream``'s channel fallback for the mic path.

    Raises :class:`CaptureUnavailableError` when no combination is accepted.
    See docs/architecture/audio.md#resolving-the-capture-format.
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
