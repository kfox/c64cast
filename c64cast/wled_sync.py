"""Broadcast WLED "Audio Sync" UDP packets synthesized from the SID.

Mode 3 of the WLED bridge: turn the music features c64cast already computes
host-side (a `MusicModulation` snapshot — level / onset / per-voice freq+gate)
into WLED's Audio Sync V2 wire format and multicast it on the LAN, so real WLED
LED matrices/strips react to whatever SID is playing on the C64 — with **no
microphone** on the WLED side. Pure stdlib UDP; no new dependency.

The consumer is WLED's `audioreactive` usermod with **Sound Sync = "Receive"**.
It listens on multicast **239.0.0.1:11988** by default; any WLED on the segment
with receive enabled picks up the same broadcast (one c64cast → many matrices).

Wire format (verified against WLED `usermods/audioreactive/audio_reactive.cpp`,
the `__attribute__((packed))` `audioSyncPacket`, V2 header ``"00002"``):

    offset  type       field
    0       char[6]    header "00002\\0"
    6       uint8[2]   reserved (compiler gap)
    8       float      sampleRaw     (volume, ~0..255)
    12      float      sampleSmth    (smoothed volume, ~0..255)
    16      uint8      samplePeak    (0 / 1 transient flag)
    17      uint8      reserved
    18      uint8[16]  fftResult     (16-band GEQ, each 0..255)
    34      uint16     reserved
    36      float      FFT_Magnitude
    40      float      FFT_MajorPeak (dominant freq, Hz, clamped 1..11025)

44 bytes, little-endian (the ESP32 is LE). `struct.calcsize` asserts the size at
import so a format typo can't ship silently.

Mapping rationale (see docs/architecture.md): the SID has no FFT, but it *does*
expose per-voice oscillator frequency + gate, which is exactly what a 16-band
graphic-EQ wants — each sounding voice lights the GEQ bin its note falls in, at
the tune's current envelope level. `samplePeak` (the transient flag most WLED
audio effects key off) is derived by the *broadcaster* from note onsets, so it
fires even for a source that reports `onset == 0` (WaveformScene).
"""

from __future__ import annotations

import contextlib
import logging
import math
import socket
import struct
from collections.abc import Callable

from ._pollthread import PollThread
from .modulation import MusicModulation

log = logging.getLogger(__name__)

# WLED audioreactive defaults (audio_reactive.cpp).
WLED_MULTICAST_GROUP = "239.0.0.1"
WLED_DEFAULT_PORT = 11988

# V2 audioSyncPacket: header + gaps + two floats + peak + 16-band GEQ + two
# floats. The `x` pad bytes are the compiler gaps WLED's packed struct carries;
# struct.pack ignores them (they go out as zeros), matching `reserved*`.
_V2_HEADER = b"00002\x00"
_PACKET_FMT = "<6s2xffBx16B2xff"
assert struct.calcsize(_PACKET_FMT) == 44, "WLED audioSyncPacket must be 44 bytes"

_NUM_GEQ = 16  # WLED NUM_GEQ_CHANNELS

# GEQ band span: log-spaced 40 Hz .. 10 kHz across the 16 bins. Covers the SID's
# musical range; the exact edges only affect which bin a note lights, not
# correctness.
_GEQ_LO_HZ = 40.0
_GEQ_HI_HZ = 10000.0
_GEQ_LOG_SPAN = math.log(_GEQ_HI_HZ / _GEQ_LO_HZ)

# WLED clamps FFT_MajorPeak to this range for its effects.
_MAJOR_PEAK_MIN_HZ = 1.0
_MAJOR_PEAK_MAX_HZ = 11025.0

# onset >= this reads as a transient (samplePeak). Onset spikes to 1.0 on a note
# attack and decays, so 0.5 catches the attack for the frame or two it's hot.
_ONSET_PEAK_THRESHOLD = 0.5


def _freq_to_geq_bin(freq_hz: float) -> int:
    """Map an oscillator frequency to a 0..15 GEQ band (log-spaced, clamped)."""
    if freq_hz <= _GEQ_LO_HZ:
        return 0
    idx = int(_NUM_GEQ * math.log(freq_hz / _GEQ_LO_HZ) / _GEQ_LOG_SPAN)
    return max(0, min(_NUM_GEQ - 1, idx))


def build_audio_sync_packet(mod: MusicModulation, sample_peak: bool) -> bytes:
    """Serialize one `MusicModulation` snapshot into a 44-byte WLED V2 packet.

    Pure (no I/O) so it's unit-testable against the documented struct format.
    `sample_peak` is the transient flag the caller derives (see the module
    docstring)."""
    level = max(0.0, min(1.0, mod.level))
    vol = level * 255.0
    mag = round(level * 255.0)

    fft = [0] * _NUM_GEQ
    major_peak = _MAJOR_PEAK_MIN_HZ
    for freq, gate in zip(mod.voice_freqs, mod.voice_gates, strict=True):
        if not gate or freq <= 0.0:
            continue
        b = _freq_to_geq_bin(freq)
        fft[b] = max(fft[b], mag)
        # Dominant partial: the highest-frequency active voice (the lead, most
        # often) — we have no per-voice amplitude to rank by.
        if freq > major_peak:
            major_peak = freq
    major_peak = max(_MAJOR_PEAK_MIN_HZ, min(_MAJOR_PEAK_MAX_HZ, major_peak))

    return struct.pack(
        _PACKET_FMT,
        _V2_HEADER,
        vol,  # sampleRaw
        vol,  # sampleSmth
        1 if sample_peak else 0,  # samplePeak
        *fft,  # fftResult[16]
        float(mag),  # FFT_Magnitude
        float(major_peak),  # FFT_MajorPeak
    )


class WledAudioSyncBroadcaster:
    """A daemon thread that emits WLED Audio Sync packets at a fixed rate.

    Feed it a `features_fn` (typically the playlist's active-scene feature
    accessor) and it pulls a `MusicModulation` each tick, maps it to a packet,
    and `sendto`s the WLED multicast group (or a unicast `host`). It is
    process-wide and source-agnostic: whichever SID-driven scene is on screen
    supplies the features.

    Peak detection lives here (not in the sources) so it works uniformly: a
    packet's `samplePeak` fires when the source reports a transient
    (`onset > threshold`) OR when any voice gate rose since the previous packet.
    The latter gives WaveformScene — which reports `onset == 0` — real
    note-attack flashes for free at the broadcast sampling rate.
    """

    def __init__(
        self,
        features_fn: Callable[[], MusicModulation | None],
        *,
        host: str | None = None,
        port: int = WLED_DEFAULT_PORT,
        rate_hz: float = 50.0,
    ) -> None:
        self._features_fn = features_fn
        self._host = host or WLED_MULTICAST_GROUP
        self._port = port
        self._rate_hz = max(1.0, rate_hz)
        self._is_multicast = self._host == WLED_MULTICAST_GROUP
        self._sock: socket.socket | None = None
        self._poll: PollThread | None = None
        self._prev_gates: tuple[bool, ...] = (False, False, False)
        self._send_errors = 0

    def start(self) -> None:
        """Open the UDP socket and start the emit thread. No-op if running."""
        if self._poll is not None and self._poll.is_running():
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self._is_multicast:
            # TTL 1 keeps the multicast on the local segment (the usual WLED
            # setup); no group join needed to *send*.
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        log.info(
            "WLED audio sync: broadcasting to %s:%d at %.0f Hz (%s)",
            self._host,
            self._port,
            self._rate_hz,
            "multicast" if self._is_multicast else "unicast",
        )
        self._poll = PollThread(
            self._emit, period=1.0 / self._rate_hz, name="wled-sync", run_first=False
        )
        self._poll.start()

    def stop(self) -> None:
        """Stop the emit thread and close the socket. Safe to call more than once."""
        if self._poll is not None:
            self._poll.stop()
            self._poll = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _derive_peak(self, mod: MusicModulation) -> bool:
        """True on a reported transient or a rising gate since the last packet."""
        gates = tuple(mod.voice_gates)
        rose = any(g and not p for g, p in zip(gates, self._prev_gates, strict=True))
        self._prev_gates = gates
        return mod.onset >= _ONSET_PEAK_THRESHOLD or rose

    def _emit(self) -> None:
        """One tick: pull features (skip if none), build + send a packet.
        Socket errors are counted and logged once — a broadcast hiccup must
        never disturb playback."""
        if self._sock is None:
            return
        mod = self._features_fn()
        if mod is None:
            return
        peak = self._derive_peak(mod)
        packet = build_audio_sync_packet(mod, peak)
        try:
            self._sock.sendto(packet, (self._host, self._port))
        except OSError as e:
            self._send_errors += 1
            if self._send_errors == 1:
                log.warning(
                    "WLED audio sync: send to %s:%d failed (%s) — will keep trying quietly",
                    self._host,
                    self._port,
                    e,
                )
