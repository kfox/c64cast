"""SID-driven music feature stream for reactive generative visuals.

`SidFeatureStream` is the cheapest music-feature source we have: it runs the
same SID file the U64 is playing in parallel on a host-side 6502
([SidHostEmu](sid_host_emu.py)) and reads per-voice envelope / frequency / gate
state straight out of the emulated `$D400-$D418` shadow — no FFT, no
onset-detection on a raw audio signal, because the features are already computed
by the emulator we know how to run. It mirrors WaveformScene's poll-thread
pattern (a `PollThread` ticking PLAY at the tune's real rate, with wall-clock
catch-up) but, instead of an oscilloscope trace, distills the state into a small
[MusicModulation](modulation.py) snapshot the generators read.

It is entirely host-side: the real chip plays autonomously on the U64 and the
poll thread only steps a pure-Python emulator, so this adds **zero** U64 bus
traffic — important, because a SID-audio SourceScene is already forced onto
host-DMA and bitmap displays already run at half-rate.

`bpm` is an onset-rate proxy, not a true beat tracker: it EMAs the interval
between note onsets (gate-on edges + hard-restarts), folded into a plausible
tempo band. `beat_phase` is the running integral of `bpm/60`, so a jittery
estimate never causes a phase discontinuity (the cycle rate it drives stays
smooth). That math lives in `modulation.TempoEstimator`, shared with the
audio-input analyzer ([audio_features.py](audio_features.py)) so both producers
report tempo identically; a real beat tracker can be dropped in behind it later.

This stream reports no `bands` — it reads envelopes, not a spectrum, so
`MusicModulation.bands` stays empty on the SID path (see modulation.py).
"""

from __future__ import annotations

import logging
import math
import threading
import time

from ._pollthread import PollThread
from .c64 import SID, cpu_clock
from .modulation import MusicModulation, TempoEstimator
from .sid_host_emu import SidHostEmu
from .sidemu import ACCUMULATOR_RANGE, SIDEmulator

log = logging.getLogger(__name__)


class SidFeatureStream:
    """Persistent host-side SID emulator + poll thread exposing a live
    `MusicModulation` snapshot. Construct cheaply, then `start()` to spin up the
    emulator + thread, `features()` from the render thread, `stop()` at teardown.

    Thread model: the poll thread owns the `SidHostEmu` (only it touches it) and
    updates the shared `SIDEmulator` + feature accumulators under `_lock`;
    `features()` reads them under the same lock so the render thread always sees
    a consistent snapshot.
    """

    # Catch-up safety: never run more than this many PLAY passes in a single
    # poll wakeup (mirrors WaveformScene._MAX_CATCHUP_TICKS — bounds a long
    # scheduler stall to a fixed amount of host CPU rather than a stampede).
    _MAX_CATCHUP_TICKS = 120
    # PLAY passes to probe a tune's multispeed rate (see _detect_play_rate_hz).
    _RATE_PROBE_TICKS = 64

    # Onset envelope time constant (seconds). The per-tick decay factor is
    # exp(-dt/τ); τ≈0.18 s gives a brief, visible pulse that fades over ~3-4
    # frames at 60 Hz.
    _ONSET_TAU_S = 0.18

    def __init__(
        self,
        sid_bytes: bytes,
        song: int = 0,
        *,
        system: str = "NTSC",
        reg_poll_hz: float | None = None,
    ) -> None:
        self._sid_bytes = sid_bytes
        self._song = song
        self._system = system
        self._clock = cpu_clock(system)
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        self._user_reg_poll_hz = reg_poll_hz

        self._lock = threading.Lock()
        self._emulator: SIDEmulator | None = None
        self._host_emu: SidHostEmu | None = None
        self._poll: PollThread | None = None

        # Tick cadence (set in start()).
        self._reg_poll_hz = self._video_hz
        self._poll_dt = 1.0 / self._video_hz
        self._onset_decay = 0.0

        # Wall-clock catch-up bookkeeping (see _poll).
        self._sid_start_time = 0.0
        self._ticks_done = 0

        # Feature accumulators (reset in start()).
        self._tick_index = 0
        self._onset = 0.0
        self._tempo = TempoEstimator()
        self._prev_gate = [False] * SID.N_VOICES

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Build the persistent emulator, detect the PLAY rate, and start the
        poll thread. Safe to call once; a second call is a no-op while running.
        May raise if the SID can't be parsed/INIT'd (the caller — a SID audio
        source whose tune already passed run_sid_player — wraps this so a
        failure degrades to non-reactive playback rather than crashing)."""
        if self._poll is not None and self._poll.is_running():
            return
        self._prepare()
        self._poll = PollThread(
            self._poll_loop, period=self._poll_dt, name="sid-features", run_first=True
        )
        self._poll.start()

    def _prepare(self) -> None:
        """Build the persistent emulator + host emulator, detect the PLAY rate,
        and reset accumulators + clocks. Split out of start() so the poll thread
        is the only piece a unit test has to skip — tests call _prepare() then
        drive _process_tick directly with synthetic register snapshots."""
        self._emulator = SIDEmulator(system=self._system)
        self._host_emu = SidHostEmu(self._sid_bytes, song=self._song)

        rate = self._detect_play_rate_hz()
        if self._user_reg_poll_hz is None and abs(rate - self._video_hz) > 0.5:
            log.info(
                "music features: tune is CIA-timed (multispeed) — ticking host "
                "emulator at %.1f Hz (%.2fx video)",
                rate,
                rate / self._video_hz,
            )
        self._reg_poll_hz = float(rate)
        self._poll_dt = 1.0 / max(rate, 5.0)
        self._onset_decay = math.exp(-self._poll_dt / self._ONSET_TAU_S)

        # Reset accumulators + clocks so a fresh stream starts clean.
        self._tick_index = 0
        self._onset = 0.0
        self._tempo.reset()
        self._prev_gate = [False] * SID.N_VOICES
        self._sid_start_time = time.time()
        self._ticks_done = 0

    def stop(self) -> None:
        """Stop the poll thread (pure host-side cleanup; no U64 I/O)."""
        if self._poll is not None:
            self._poll.stop()

    # ---- poll thread --------------------------------------------------------

    def _detect_play_rate_hz(self) -> float:
        """Return the tune's effective PLAY rate in Hz. A user override wins;
        otherwise probe a THROWAWAY emulator (so the real one keeps its song
        position), since many multispeed players only write CIA #1 Timer A on
        the first PLAY — reading the rate straight after INIT would mis-detect
        them as plain vsync and tick the features at half the song's real pace.
        Mirrors WaveformScene._detect_play_rate_hz."""
        if self._user_reg_poll_hz is not None:
            return float(self._user_reg_poll_hz)
        probe = SidHostEmu(self._sid_bytes, song=self._song)
        rate = probe.play_rate_hz(self._video_hz, self._clock)
        for _ in range(self._RATE_PROBE_TICKS):
            if abs(rate - self._video_hz) > 0.5:
                break  # multispeed Timer A latch seen — rate is known
            probe.tick_play()
            rate = probe.play_rate_hz(self._video_hz, self._clock)
        return float(rate)

    def _poll_loop(self) -> None:
        """Advance the host emulator to the PLAY-tick count wall-clock says the
        real SID has reached, processing each caught-up tick. Tick count is
        derived from elapsed wall-clock (not poll-wakeup count) so the feature
        stream tracks what the audience hears, same model as
        WaveformScene._poll_regs. The expensive py65 PLAY pass runs outside the
        lock; only the feature update (_process_tick) takes it."""
        assert self._host_emu is not None
        target = round((time.time() - self._sid_start_time) * self._reg_poll_hz)
        n = target - self._ticks_done
        if n <= 0:
            return  # ahead of / on schedule — let wall-clock catch up
        n = min(n, self._MAX_CATCHUP_TICKS)
        for _ in range(n):
            self._host_emu.tick_play()
            regs = self._host_emu.regs()
            retrig = self._host_emu.retriggers()
            self._process_tick(regs, retrig)
        self._ticks_done += n

    def _process_tick(self, regs: bytes, retrig: tuple[bool, bool, bool]) -> None:
        """Fold one PLAY tick's register snapshot into the emulator + feature
        accumulators. Pure given (regs, retrig) and the prior state, so it's
        directly unit-testable with hand-built register arrays — no thread, no
        real SID. Takes `_lock` since it mutates the shared emulator + scalars
        that `features()` reads."""
        assert self._emulator is not None
        with self._lock:
            self._emulator.update_registers(regs, retrigger=retrig)
            self._emulator.advance_envelopes(self._poll_dt)
            self._tick_index += 1
            now = self._tick_index * self._poll_dt

            onset_now = False
            for v in range(SID.N_VOICES):
                ctrl = regs[v * SID.BYTES_PER_VOICE + SID.OFF_CONTROL]
                gate = bool(ctrl & SID.GATE)
                if (gate and not self._prev_gate[v]) or retrig[v]:
                    onset_now = True
                self._prev_gate[v] = gate

            # Decay first so a fresh onset reads as a clean 1.0.
            self._onset *= self._onset_decay
            if onset_now:
                self._onset = 1.0
                self._tempo.note_onset(now)

            self._tempo.advance(self._poll_dt)

    # ---- feature snapshot ---------------------------------------------------

    def features(self) -> MusicModulation | None:
        """Return the current music-feature snapshot, or None before start().
        Reads the shared emulator + accumulators under `_lock`."""
        if self._emulator is None:
            return None
        with self._lock:
            voices = self._emulator.voices
            clk = self._clock
            freqs = (
                voices[0].freq * clk / ACCUMULATOR_RANGE,
                voices[1].freq * clk / ACCUMULATOR_RANGE,
                voices[2].freq * clk / ACCUMULATOR_RANGE,
            )
            gates = (voices[0].gated(), voices[1].gated(), voices[2].gated())
            level = min(1.0, sum(v.envelope_level for v in voices) / SID.N_VOICES)
            return MusicModulation(
                level=level,
                onset=self._onset,
                beat_phase=self._tempo.beat_phase,
                bpm=self._tempo.bpm,
                voice_freqs=freqs,
                voice_gates=gates,
            )
