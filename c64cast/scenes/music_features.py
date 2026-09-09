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

from c64cast._pollthread import PollThread
from c64cast.hw.c64 import SID, cpu_clock
from c64cast.sid.sid_host_emu import (
    HostEmuBudget,
    SidHostEmu,
    describe_pass_cost,
    detect_play_rate_hz,
    init_truncation_notice,
    run_catchup_passes,
    sustainable_poll_period_s,
)
from c64cast.sid.sidemu import ACCUMULATOR_RANGE, SIDEmulator

from .modulation import MusicModulation, TempoEstimator

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
    # scheduler stall to a fixed amount of host CPU rather than a stampede),
    # and never spend more than this fraction of a poll period doing it. The
    # count alone is not a time bound: the tune sets what a PLAY pass costs and
    # the rate the batch is sized against, so an expensive tune could keep this
    # thread permanently busy. See sid_host_emu.run_catchup_passes.
    _MAX_CATCHUP_TICKS = 120
    _MAX_CATCHUP_PERIOD_FRACTION = 0.5
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

        # Tick cadence (set in start()). `_poll_dt` is the song time one PLAY
        # tick advances; `_poll_period` is how often the thread wakes, which is
        # the same number until a PLAY pass costs more than the rate allows.
        self._reg_poll_hz = self._video_hz
        self._poll_dt = 1.0 / self._video_hz
        self._poll_period = self._poll_dt
        self._onset_decay = 0.0

        # Wall-clock catch-up bookkeeping (see _poll).
        self._sid_start_time = 0.0
        self._ticks_done = 0
        self._catchup_warned = False

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
            self._poll_loop, period=self._poll_period, name="sid-features", run_first=True
        )
        self._poll.start()

    def _prepare(self) -> None:
        """Build the persistent emulator + host emulator, detect the PLAY rate,
        and reset accumulators + clocks. Split out of start() so the poll thread
        is the only piece a unit test has to skip — tests call _prepare() then
        drive _process_tick directly with synthetic register snapshots."""
        self._emulator = SIDEmulator(system=self._system)
        # One budget for this whole preparation: the persistent emulator's INIT
        # and the throwaway probe's. INIT is bounded in emulated cycles at 2 M,
        # which is not seconds — an unbudgeted one is the part no per-pass cap
        # was ever bounding, and this runs on the audio source's setup path.
        budget = HostEmuBudget()
        self._host_emu = SidHostEmu(self._sid_bytes, song=self._song, budget=budget)
        # Before the probe below, which INITs the same tune and would make the
        # sticky flag say nothing about this emulator. Not a refusal — see
        # init_truncation_notice.
        notice = init_truncation_notice(self._host_emu)
        if notice is not None:
            log.warning(
                "music features: %s; the reactive visuals may not match the audio",
                notice,
            )

        rate, pass_cost_s = self._detect_play_rate_hz(budget)
        if self._user_reg_poll_hz is None and abs(rate - self._video_hz) > 0.5:
            log.info(
                "music features: tune is CIA-timed (multispeed) — ticking host "
                "emulator at %.1f Hz (%.2fx video)",
                rate,
                rate / self._video_hz,
            )
        self._reg_poll_hz = float(rate)
        self._poll_dt = 1.0 / max(rate, 5.0)
        # Floored so one measured PLAY pass fits inside a fraction of a wakeup.
        # The tune sets both the rate and the pass cost, so without this the
        # catch-up batch's time bound could be smaller than one indivisible
        # pass — the thread then runs continuously and, under the GIL, takes
        # the render thread's time with it. See sustainable_poll_period_s.
        self._poll_period = sustainable_poll_period_s(
            self._poll_dt, pass_cost_s, self._MAX_CATCHUP_PERIOD_FRACTION
        )
        if self._poll_period > self._poll_dt:
            log.warning(
                "music features: %s, more than this tune's "
                "%.1f Hz PLAY rate allows; polling every %.1f ms instead — reactive "
                "visuals will lag the audio",
                describe_pass_cost(pass_cost_s),
                rate,
                self._poll_period * 1000.0,
            )
        self._onset_decay = math.exp(-self._poll_dt / self._ONSET_TAU_S)

        # Reset accumulators + clocks so a fresh stream starts clean.
        self._tick_index = 0
        self._onset = 0.0
        self._tempo.reset()
        self._prev_gate = [False] * SID.N_VOICES
        self._sid_start_time = time.time()
        self._ticks_done = 0
        self._catchup_warned = False

    def stop(self) -> None:
        """Stop the poll thread (pure host-side cleanup; no U64 I/O)."""
        if self._poll is not None:
            self._poll.stop()

    # ---- poll thread --------------------------------------------------------

    def _detect_play_rate_hz(self, budget: HostEmuBudget) -> tuple[float, float | None]:
        """Return ``(rate_hz, pass_cost_s)`` for this tune, probed on a
        THROWAWAY emulator so the real one keeps its song position.

        Many multispeed players only write CIA #1 Timer A on the first PLAY, so
        reading the rate straight after INIT would mis-detect them as plain
        vsync and tick the features at half the song's real pace. A user
        override wins the RATE, but not the measurement: what a PLAY pass costs
        belongs to the tune and the host, and it is the number `_prepare` floors
        the poll period against.

        `pass_cost_s` is ``None`` when no pass was timed — see
        sid_host_emu.detect_play_rate_hz, which both scenes share."""
        probe = SidHostEmu(self._sid_bytes, song=self._song, budget=budget)
        rate, pass_cost_s = detect_play_rate_hz(
            probe,
            video_hz=self._video_hz,
            clock_hz=self._clock,
            budget=budget,
        )
        if self._user_reg_poll_hz is not None:
            return float(self._user_reg_poll_hz), pass_cost_s
        return rate, pass_cost_s

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
        emu = self._host_emu
        batch = run_catchup_passes(
            emu,
            lambda: self._process_tick(emu.regs(), emu.retriggers()),
            ticks=n,
            seconds=self._poll_period * self._MAX_CATCHUP_PERIOD_FRACTION,
        )
        self._ticks_done += batch.passes
        if batch.passes < n or batch.overran:
            self._warn_catchup_behind(target - self._ticks_done)

    def _warn_catchup_behind(self, shortfall: int) -> None:
        """Say once that this tune's PLAY is too expensive to emulate in real
        time. The features then track the audio loosely rather than exactly;
        the condition lasts the whole stream, so the log must not.

        Fires on a short batch AND on a batch that ran every pass it was asked
        for but used its whole time bound doing it — with one pass requested,
        the pass count cannot tell those apart."""
        if self._catchup_warned:
            return
        self._catchup_warned = True
        log.warning(
            "music features: the host emulator can't keep up with this tune's PLAY "
            "rate (%.1f Hz, %d ticks behind after a catch-up batch that used its whole "
            "time bound) — reactive visuals will lag the audio",
            self._reg_poll_hz,
            max(shortfall, 0),
        )

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
