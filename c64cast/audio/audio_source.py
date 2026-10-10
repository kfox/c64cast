"""Pluggable audio sources for composable scenes.

The "audio source" building block, parallel to FrameSource: a `SourceScene`
pairs a video source with one of these, so the visual and the sound are
chosen independently.

* `NullAudioSource` — silence.
* `MicAudioSource` — the shared AudioStreamer's live mic path, reactive by
  default. With `listen_only` (`audio_source = "listen"`) it analyzes the
  input but plays no C64 audio.
* `AudioFileSource` — decodes an audio file (mp3/wav/… via PyAV) to the DAC
  or the Ultimate Audio sampler, and runs the same pre-DSP analyzer over it
  (`audio_source = "file"`).
* `SidFileAudioSource` — plays a .sid file on the real chip; the audio half
  of WaveformScene, factored out so it composes with any FrameSource.

See docs/architecture/audio.md#audio_sourcepy--audiofilesource-audio-file-reactive-source.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from c64cast._teardown import run_teardown_steps

if TYPE_CHECKING:
    from c64cast.app.config import AudioCfg, AudioFeaturesCfg
    from c64cast.hw.backend import C64Backend
    from c64cast.scenes.modulation import MusicModulation
    from c64cast.scenes.music_features import SidFeatureStream
    from c64cast.sid.sid_host_emu import HostEmuBudget, SidHeader
    from c64cast.video.modes import DisplayMode

    from .audio import AudioStreamer
    from .audio_features import AudioFeatureStream
    from .sampler import UltimateAudioSampler

log = logging.getLogger(__name__)


def heard_seconds(audio: AudioStreamer | UltimateAudioSampler) -> float:
    """How much of the audio pushed to ``audio`` has been heard, on the
    sink's clock. The sampler's clock is the wall since its gate, and a
    re-anchor plays every later sample that much past its slot, so its lag
    comes off; the DAC's clock counts the samples played and needs no
    correction. Everything that reads a sink's clock as "the sound now" (an
    audio file's analyzer and end, a video's playback clock) reads this, so
    none of them runs a re-anchor's lag ahead of the sound."""
    played = audio.position_seconds() or 0.0
    if getattr(audio, "is_sampler", False):
        # At this position's read head: the clock moves between the two
        # reads, and inside a re-anchor's hold that stepped the sample back.
        played -= cast("UltimateAudioSampler", audio).reanchor_lag_seconds(played)
    return played


# Following the DAC's drain (DrainFollower). The window is read from
# DRAIN_FOLLOW_WARMUP_S after the clock starts, while the servo still settles
# the ring lead; a window restarts at an underrun, at a lost write, and across
# a span of DRAIN_FOLLOW_STALL_S or more over which the clock ran at under half
# the slowest drain followed (a stalled link, a pause), since none is the
# drain. The span is measured from an anchor rather than between neighboring
# readings: a push into a stalled sink returns after QUEUE_PUT_TIMEOUT_S
# (0.2 s), so readings across a stall come closer together than the span.
# The drain is measured, not converged on, so the deadband only keeps
# estimator noise from rebuilding the resampler: at 0.005 a steady 0.94 drain
# on hardware read 0.935..0.945 window to window and rebuilt it every few
# seconds. DRAIN_FOLLOW_RETUNE_S spaces the rebuilds.
DRAIN_FOLLOW_WARMUP_S = 3.0
DRAIN_FOLLOW_WINDOW_S = 4.0
DRAIN_FOLLOW_DEADBAND = 0.01
DRAIN_FOLLOW_STALL_S = 0.25
DRAIN_FOLLOW_RETUNE_S = 2.0
DRAIN_FOLLOW_MIN = 0.80


class DrainFollower:
    """The fraction of real time a DAC sink's clock advances at, measured
    from (wall, clock) readings, as the scale a file is resampled by.

    The `$D418` DAC plays one sample per NMI the 6510 services, and the
    scene's host writes on the shared bus cost it NMIs, so the sink drains
    below its armed rate. Resampled to ``effective_rate × drain``, a second
    of the track is the samples the NMI plays in a second, so it plays at
    its own speed and pitch. Not by raising the NMI rate: that moves in
    latch steps (≈1.2 % at 12 kHz), and on a bitmap mode the drain does not
    rise with it. See
    docs/architecture/audio.md#audio_sourcepy--audiofilesource-audio-file-reactive-source.

    Pure: the caller supplies the time, the clock and the trust stamp."""

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale
        self._marks: deque[tuple[float, float]] = deque()
        self._started_at: float | None = None
        self._trust: object = None
        # (wall, clock) the stall check measures its span from.
        self._stall_anchor: tuple[float, float] | None = None
        self._last_retune = -math.inf

    def observe(self, now: float, clock_s: float, trust: object) -> float | None:
        """Take one reading; returns the new scale when it moved by the
        deadband or more, else None. ``trust`` is anything that changes at
        an underrun (a window across one is not the drain)."""
        marks = self._marks
        if clock_s <= 0.0:
            # The consumer has not started: nothing drains yet.
            self._started_at = None
            self._stall_anchor = None
            marks.clear()
            return None
        if self._started_at is None:
            self._started_at = now
        if now - self._started_at < DRAIN_FOLLOW_WARMUP_S:
            marks.clear()
            self._trust = trust
            self._stall_anchor = (now, clock_s)
            return None
        if self._stalled(now, clock_s) or trust != self._trust:
            marks.clear()
        self._trust = trust
        marks.append((now, clock_s))
        while len(marks) > 2 and marks[1][0] <= now - DRAIN_FOLLOW_WINDOW_S:
            marks.popleft()
        (w0, c0), (w1, c1) = marks[0], marks[-1]
        if (
            w1 - w0 < 0.75 * DRAIN_FOLLOW_WINDOW_S
            or now - self._last_retune < DRAIN_FOLLOW_RETUNE_S
        ):
            return None
        drain = min(1.0, max(DRAIN_FOLLOW_MIN, (c1 - c0) / (w1 - w0)))
        if abs(drain - self.scale) < DRAIN_FOLLOW_DEADBAND:
            return None
        self.scale = drain
        self._last_retune = now
        return drain

    def _stalled(self, now: float, clock_s: float) -> bool:
        """Whether the clock ran at under half the slowest drain followed over
        the span since the anchor, once that span reaches DRAIN_FOLLOW_STALL_S;
        the anchor then moves to this reading."""
        anchor = self._stall_anchor
        if anchor is None:
            self._stall_anchor = (now, clock_s)
            return False
        w_a, c_a = anchor
        span = now - w_a
        if span < DRAIN_FOLLOW_STALL_S:
            return False
        self._stall_anchor = (now, clock_s)
        return clock_s - c_a < 0.5 * DRAIN_FOLLOW_MIN * span


@runtime_checkable
class AudioSource(Protocol):
    """How a SourceScene makes sound. `setup`/`teardown` bracket the scene;
    `position_seconds` exposes a master clock if the source owns one (None when
    it doesn't, e.g. a free-running mic); `features` exposes a live music-feature
    snapshot for reactive visuals (None when the source has no feature stream).

    `resets_display` is True when `setup()` disturbs the VIC display state — a
    SID source kicks its player via the firmware's run_prg, which re-inits the
    machine back to text mode. SourceScene re-asserts the display mode AFTER
    such a source starts so a bitmap display isn't left rendering text.

    `finished` is True once a finite source has played everything it had, and
    SourceScene ends the scene on it. Sources with no end report False."""

    wants_audio_lock: bool
    resets_display: bool

    @property
    def finished(self) -> bool: ...

    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def position_seconds(self) -> float | None: ...
    def features(self) -> MusicModulation | None: ...


class NullAudioSource:
    """Silent. The default for a scene with no audio."""

    wants_audio_lock = False
    resets_display = False
    finished = False

    def setup(self) -> None:
        return None

    def teardown(self) -> None:
        return None

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return None


class MicAudioSource:
    """Streaming sampled audio from the live microphone via the shared
    AudioStreamer. `display_mode` is consulted only to mirror WebcamScene's
    REU-pump coordination: when a bitmap mode installs the merged $0314
    dispatcher, the mic REU pump must skip its own IRQ hook.

    Music-reactive visuals: when `reactive` (default True), setup() installs a
    pre-DSP `AnalysisTap` on the streamer and starts an
    [AudioFeatureStream](audio_features.py) over it, so `features()` reports live
    level / onset / band energies / tempo from whatever is being played into the
    input — an iRig, a mixer feed, a mic. That is the same `MusicModulation` the
    SID path produces, so generators, the effect chain and the WLED broadcaster
    all react without knowing which producer is behind it. `reactive=False` (or
    a startup failure) leaves features() returning None and the visuals purely
    time-driven; audio still streams to the DAC either way.

    `listen_only` (audio_source = "listen") captures the input for analysis but
    plays NO C64 audio: setup() calls `start_listen` instead of `start_mic`, so
    nothing reaches the 4-bit DAC. Because it is freed from the DAC's sample
    rate, the input opens at `features_cfg.listen_sample_rate` (44.1 kHz by
    default) and the analyzer is built to match — full-bandwidth audio for
    cleaner onset/treble detection than the 12 kHz DAC path allows. This is the
    VJ case: the real music comes from a PA, and only the visuals track it."""

    # Uncorrelated input, not the ensemble's SID spotlight (as WebcamScene).
    wants_audio_lock = False
    resets_display = False  # the mic path doesn't touch the VIC
    finished = False  # live input has no end

    def __init__(
        self,
        audio: AudioStreamer,
        audio_cfg: AudioCfg,
        display_mode: DisplayMode | None = None,
        *,
        reactive: bool = True,
        listen_only: bool = False,
        features_cfg: AudioFeaturesCfg | None = None,
    ):
        self._audio = audio
        self._cfg = audio_cfg
        self._display_mode = display_mode
        self._reactive = reactive
        self._listen_only = listen_only
        self._features_cfg = features_cfg
        self._features: AudioFeatureStream | None = None

    def setup(self) -> None:
        from c64cast.app.config import AudioFeaturesCfg
        from c64cast.video.modes_irq import reu_pump_skips_irq_hook

        skip_hook = (
            not self._listen_only
            and self._audio.use_reu_pump
            and reu_pump_skips_irq_hook(self._display_mode)
        )

        fcfg = self._features_cfg or AudioFeaturesCfg()
        analyzer_rate = (
            float(fcfg.listen_sample_rate) if self._listen_only else self._audio.sample_rate
        )
        # Before capture starts, so the first callbacks already reach it.
        self._start_features(fcfg, analyzer_rate)
        if self._listen_only:
            self._audio.start_listen(
                self._cfg.device,
                self._cfg.mic_sensitivity,
                sample_rate=int(analyzer_rate),
            )
        else:
            self._audio.start_mic(
                self._cfg.device,
                self._cfg.mic_sensitivity,
                self._cfg.noise_gate,
                skip_irq_vector_hook=skip_hook,
            )

    def _start_features(self, cfg: AudioFeaturesCfg, sample_rate: float) -> None:
        """Spin up the pre-DSP analyzer at `sample_rate`. A failure here must not
        cost the user their audio, so it degrades to non-reactive (same contract
        as SidFileAudioSource.setup)."""
        if not self._reactive:
            return
        from .audio_features import AnalysisTap, AudioFeatureStream

        try:
            tap = AnalysisTap(size=max(cfg.fft_size * 4, 4096))
            stream = AudioFeatureStream(
                tap,
                sample_rate,
                n_bands=cfg.bands,
                fft_size=cfg.fft_size,
                poll_hz=cfg.poll_hz,
                onset_sensitivity=cfg.onset_sensitivity,
            )
            self._audio.analysis_sink = tap.push
            stream.start()
        except Exception:
            log.exception(
                "mic audio: feature stream failed to start — visuals will not "
                "react to the input (audio continues)"
            )
            self._audio.analysis_sink = None
            return
        self._features = stream

    def teardown(self) -> None:
        # Unhook the sink before the streamer stops, so no callback can push
        # into a tap whose analyzer thread is already going away.
        self._audio.analysis_sink = None
        features, self._features = self._features, None
        steps: list[tuple[str, Callable[[], object]]] = []
        if features is not None:
            steps.append(("feature stream stop", features.stop))
        steps.append(("audio stop", self._audio.stop))
        run_teardown_steps(log, type(self).__name__, steps)

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None


class AudioFileSource:
    """Decode an audio file (mp3/wav/flac/… via PyAV) to the C64's audio output
    and run the pre-DSP analyzer over it, so a generative/test-pattern visual
    reacts to the track. The audio half of `c64cast tune.mp3`
    (audio_source = "file").

    **Backend.** The audio object is whatever `scene_factory.build_scene` resolved for
    the run: on a sampler-capable U64 with `[audio].backend` = auto/sampler it is
    an off-bus `UltimateAudioSampler` (16-bit PCM straight from REU — no
    $D418/NMI/4-bit quantization/DSP, and immune to the CPU-freeze that host-DMA
    RAM writes inflict on the NMI DAC); otherwise the shared 4-bit `$D418`
    `AudioStreamer`. The 4-bit DAC path is intrinsically lo-fi and — because its
    NMI service is jittered by every host-DMA transfer — audibly staticky on the
    U64 regardless of display mode, so the sampler is the strongly preferred path
    (HW-measured 2026-07-24). Both satisfy the same scene-facing contract
    (`sample_rate` / `push_samples` / `position_seconds` / `stop` /
    `analysis_sink`), so this source drives either polymorphically.

    Mechanism: a background thread demuxes + resamples the file to the audio
    object's mono int16 rate and feeds its `push_samples`, exactly as
    `AVFileSource` feeds a video's audio — push_samples both encodes the samples
    for the C64 and (pre-DSP) forwards them to `analysis_sink`, so the *same*
    analyzer the mic path uses drives the visuals off the decoded audio. Playback
    is real-time paced by push_samples' backpressure (queue-full block), so the
    decode thread tracks the consumption rate once the sink's queue is full. The
    sampler's queue holds 256 decoded chunks, about 23 s of a 44 kHz WAV, so a
    shorter file is decoded whole before anything plays.

    `wants_audio_lock=False`: like the mic/video paths, a file is not the
    ensemble's SID spotlight (`scene_factory.build_scene` also suppresses its DAC audio
    in ensemble mode). `resets_display=False`: the DAC path never touches the VIC.

    **End of track.** `finished` turns True once the decoder has reached the
    end of the file (or failed) and the sink's clock has played out the audio
    it pushed, so the scene ends when the sound does rather than when the container
    header says it should. `duration_s` is the header's figure for the file the
    last `setup()` picked; it only decides whether there is an end to wait for
    (see `SourceScene.duration_follows_audio`). A startup failure degrades to
    non-reactive silence with the scene intact, the same contract as
    `MicAudioSource`/`SidFileAudioSource`.
    """

    wants_audio_lock = False
    resets_display = False

    # Mirrors SidFileAudioSource: a file that won't open is skipped.
    _MAX_PICK_ATTEMPTS = 8

    # `finished` waits for the sink's clock to reach the length of the audio
    # pushed, so it cannot be scheduled once at the decoder's end: the
    # sampler's queue is counted in chunks, and 256 decoded chunks hold about
    # 23 s of a 44 kHz WAV, so a short file is decoded whole before the ring is
    # gated and its clock reads 0. This grace bounds the wait for a sink clock
    # that never gets there, past the audio still unplayed when decode ended;
    # it covers the sampler's bring-up (the ring prefill, and up to 2 s of
    # prebuffer wait) between the end of decoding and the gate.
    _DRAIN_GRACE_S = 5.0
    # The most content lag gained after decoding ended that the deadline waits
    # out. A sampler's lag grows with each re-anchor, so a
    # deadline counting all of it could hold a scene open without end; past
    # this, `finished` ends the scene anyway and says so. The lag already
    # there when decoding ended counts whole: it is tail still queued, not
    # growth, and capped it cut that tail on a long slow stream.
    _MAX_COUNTED_LAG_S = 10.0

    # How far behind the decoder the sampler's analyzer can read: its queue
    # holds 256 pushes of at most _MAX_PUSH_S (25.6 s) plus its 1 s ring lead.
    # Past this the analyzer reads silence and says so once, rather than audio
    # from the wrong moment. The DAC's sizes itself (_feature_history_samples).
    _FEATURE_HISTORY_S = 30.0

    # The longest single push. The sampler's queue counts pushes, not
    # samples, and a decoded frame pushed whole (a large-block FLAC frame is
    # up to 65535 samples) let it hold minutes, past the analyzer's history.
    _MAX_PUSH_S = 0.1

    def __init__(
        self,
        audio: AudioStreamer | UltimateAudioSampler,
        file: str,
        *,
        reactive: bool = True,
        features_cfg: AudioFeaturesCfg | None = None,
    ):
        self._audio = audio
        # The sampler's start() blocks collecting a prebuffer, so setup()
        # starts the decode thread FIRST to feed it.
        self._is_sampler = bool(getattr(audio, "is_sampler", False))
        self.file_spec = file
        self._reactive = reactive
        self._features_cfg = features_cfg
        self._path: str = ""
        # Read by build_scene to size the scene; 0.0 = container said nothing.
        self.duration_s: float = 0.0
        self._features: AudioFeatureStream | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # (seconds of audio pushed, monotonic deadline, the sink's content lag
        # then) once decoding has ended; None while decoding. The deadline
        # leaves out the content lag, which `finished` adds as it reads it,
        # counting at most `_MAX_COUNTED_LAG_S` past the lag recorded here.
        # Written once by the decode thread, read by the playlist thread.
        self._end: tuple[float, float, float] | None = None
        # Set once `finished` has logged that the lag cap ended the scene, so
        # the playlist's polling logs it once per decode.
        self._lag_cap_logged = False
        # The DAC drain the last activation followed: the next starts from it
        # rather than playing its first window slow. Decode thread only.
        self._drain_scale = 1.0
        # At build time, so a misconfigured single scene raises there
        # (parity with SidFileAudioSource.__init__).
        self._pick_and_probe()

    def _pick_and_probe(self) -> None:
        """Re-resolve the spec, shuffle, and probe the first file that opens.
        Sets self._path + self.duration_s. Raises if none open."""
        from c64cast.app.scene_factory import AUDIO_EXTS, resolve_file_spec
        from c64cast.video.video import av_open, ensure_pyav

        if not ensure_pyav():
            raise RuntimeError(
                "PyAV not installed; install with `uv tool install --force 'c64cast[all]'`"
            )
        candidates = resolve_file_spec(self.file_spec, AUDIO_EXTS, label="audio file")
        pool = list(candidates)
        random.shuffle(pool)
        last_error: Exception | None = None
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                container = av_open(path)
                try:
                    if not container.streams.audio:
                        raise ValueError(f"no audio stream in {os.path.basename(path)}")
                    self.duration_s = container.duration / 1_000_000 if container.duration else 0.0
                finally:
                    container.close()
            except Exception as e:  # noqa: BLE001 — any open/probe failure → try next
                log.warning("audio file: skipping %s: %s", os.path.basename(path), e)
                last_error = e
                continue
            self._path = path
            if len(candidates) > 1:
                log.info(
                    "audio file: picked %s from %d candidates",
                    os.path.basename(path),
                    len(candidates),
                )
            return
        raise ValueError(
            f"audio file: file spec {self.file_spec!r} resolved to "
            f"{len(candidates)} candidate(s) but none could be opened; "
            f"last error: {last_error}"
        )

    def setup(self) -> None:
        """Re-pick from the (re-resolved) pool, install the analyzer, and spin up
        the decode→audio thread. Never raises on a decode/analyzer hiccup —
        degrades to non-reactive so the visual keeps running. Plenty else does
        escape, though, including a file spec that resolves to nothing openable,
        a decode thread from the last activation still running, a host too short
        of threads to start a new one, and whatever the audio bring-up raises
        over the link. `SourceScene.setup` catches all of it, logs, and flips
        `is_done` so the playlist advances.

        Ordering differs by backend, by what each bring-up call *waits* for
        rather than by whether it touches the link — both do. The 4-bit DAC's
        `start_for_external_source` uploads the NMI routine and the ring and
        returns without waiting on a producer, so it goes before the decode
        thread. The sampler's `start()` blocks up to ~2 s collecting a prebuffer
        from `push_samples`, so the decode thread must already be feeding it —
        start decode FIRST, then bring the ring up. `push_samples` accepts data
        before the ring is gated (it enqueues, blocking only when full), so the
        prebuffer fills promptly and playback starts without the empty-prebuffer
        stall.

        If a previous decode thread outlives teardown, setup raises rather than
        clearing its stop event or starting another one. Teardown stops the
        audio sink before joining, which releases a sampler producer blocked on
        a full queue; a thread that still survives the bounded join remains
        referenced, so this source cannot stack a second decoder behind it."""
        if self._thread is not None:
            if self._thread.is_alive():
                log.error("audio file: previous decode thread is still running; refusing restart")
                raise RuntimeError("previous audio-file decode thread is still running")
            self._thread = None
        self._pick_and_probe()
        self._stop.clear()
        self._end = None
        self._lag_cap_logged = False
        self._start_features()
        if self._is_sampler:
            # The sampler is reused by every activation of this scene, so it is
            # armed before the decoder can push into it.
            cast("UltimateAudioSampler", self._audio).arm()
            self._start_decode_thread()
            self._audio.start_for_external_source()
        else:
            self._audio.start_for_external_source()
            self._start_decode_thread()
        log.info(
            "audio file: %s → %s @ %dHz%s",
            os.path.basename(self._path),
            "sampler" if self._is_sampler else "DAC",
            self._audio.sample_rate,
            " (reactive)" if self._features is not None else "",
        )

    def _start_decode_thread(self) -> None:
        """Start the decode thread, and publish it only once it is running.

        A thread published before `start()` is one `teardown` can reach before
        it has ever run, and `Thread.join` raises on those. Publishing second
        means a host out of threads leaves nothing behind to trip over.

        `PollThread.start` takes the same order for a neighboring reason, and
        the difference is worth knowing before this is copied: it is guarding
        the publish window against a `stop()` arriving from another thread, and
        it closes that window with an RLock this has no equivalent of. What
        stands in for the lock here is that `setup` and `teardown` both run on
        the playlist's worker thread, so nothing can land between the two
        statements. A caller tearing a source down from anywhere else would
        need the lock.
        """
        thread = threading.Thread(target=self._decode_loop, daemon=True, name="audio-file-decode")
        thread.start()
        self._thread = thread

    def _start_features(self) -> None:
        """Install the pre-DSP analyzer at the streamer's DAC rate (what the DAC
        actually plays, like the mic path). A failure must not cost playback.

        The decoder runs the sink's whole queue and ring ahead of what is
        heard, so the analyzer reads the window ending at the sink's played
        position rather than the newest one; the tap keeps enough history to
        reach back that far."""
        if not self._reactive:
            return
        from c64cast.app.config import AudioFeaturesCfg

        from .audio_features import AnalysisTap, AudioFeatureStream

        cfg = self._features_cfg or AudioFeaturesCfg()
        audio = self._audio
        # The tap is indexed in pushed samples, which the decoder resamples to
        # this rate, and the sink's clock divides by the same one.
        rate = float(audio.effective_rate or audio.sample_rate)

        def played_index() -> float:
            return max(self._heard_seconds(), 0.0) * rate

        try:
            history = self._feature_history_samples(rate)
            tap = AnalysisTap(size=max(cfg.fft_size * 4, 4096, history + cfg.fft_size))
            stream = AudioFeatureStream(
                tap,
                self._audio.sample_rate,
                n_bands=cfg.bands,
                fft_size=cfg.fft_size,
                poll_hz=cfg.poll_hz,
                onset_sensitivity=cfg.onset_sensitivity,
                play_position=played_index,
            )
            self._audio.analysis_sink = tap.push
            stream.start()
        except Exception:
            log.exception(
                "audio file: feature stream failed to start — visuals will not "
                "react (audio continues)"
            )
            self._audio.analysis_sink = None
            return
        self._features = stream

    def _feature_history_samples(self, rate: float) -> int:
        """How many samples behind the newest push the analyzer may read.

        The DAC's figure is twice what it can hold unplayed: its queue's soft
        cap, one push over it, the worker's two chunks in hand, and the ring.
        A flat _FEATURE_HISTORY_S kept 1.4 MB at 12 kHz for a lag that never
        passes about 2 s."""
        if self._is_sampler:
            return int(rate * self._FEATURE_HISTORY_S)
        from .audio_handlers import CHUNK_SIZE, MAX_QUEUED_SAMPLES, RING_BUFFER_SIZE

        unplayed = (
            MAX_QUEUED_SAMPLES + self._max_push_samples(rate) + 2 * CHUNK_SIZE + RING_BUFFER_SIZE
        )
        return 2 * unplayed

    def _max_push_samples(self, rate: float) -> int:
        return max(1, int(rate * self._MAX_PUSH_S))

    def _decode_loop(self) -> None:
        """Demux + resample the file to mono int16 at the DAC rate and feed
        push_samples (which DAC-encodes AND taps the analyzer). Real-time paced by
        push_samples' queue-full block. Ends at EOF or when `_stop` is set."""
        from c64cast.video.video import av_open

        # effective_rate, not sample_rate: the rate the sink really
        # consumes at, so the servo starts from zero standing error.
        rate = int(round(self._audio.effective_rate)) or self._audio.sample_rate
        follower = self._new_drain_follower()
        pushed = 0
        try:
            container = av_open(self._path)
        except Exception:
            log.exception("audio file: could not open %s for decode", self._path)
            self._mark_decode_done(0)
            return
        try:
            import av  # noqa: PLC0415  (optional extra; only reached when PyAV present)

            scale = follower.scale if follower is not None else 1.0
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self._drained_rate(rate, scale)
            )
            a_stream = container.streams.audio[0]
            for packet in container.demux(a_stream):
                if self._stop.is_set():
                    return
                for frame in packet.decode():
                    retuned = self._observe_drain(follower, rate)
                    if retuned is not None:
                        # The old filter's tail first, or its last few
                        # milliseconds are lost at every retune.
                        for resampled in resampler.resample(None):
                            if self._stop.is_set():
                                return
                            pushed += self._push_frame(resampled)
                        resampler = av.AudioResampler(
                            format="s16", layout="mono", rate=self._drained_rate(rate, retuned)
                        )
                    for resampled in resampler.resample(frame):
                        if self._stop.is_set():
                            return
                        pushed += self._push_frame(resampled)
            # The resampler holds back its filter's tail until it is flushed;
            # without this the last few milliseconds of every track are lost.
            for resampled in resampler.resample(None):
                if self._stop.is_set():
                    return
                pushed += self._push_frame(resampled)
            log.info("audio file: %s reached end of track", os.path.basename(self._path))
        except Exception:
            if not self._stop.is_set():
                log.exception("audio file: decode of %s failed", os.path.basename(self._path))
        finally:
            container.close()
            if follower is not None:
                self._drain_scale = follower.scale
        if not self._stop.is_set():
            self._mark_decode_done(pushed)

    def _new_drain_follower(self) -> DrainFollower | None:
        """A follower for the DAC sink's drain, or None where there is none
        to follow: the sampler plays off the bus, at its own clock, and a sink
        without underrun telemetry offers no window to trust."""
        if self._is_sampler or not callable(getattr(self._audio, "stats", None)):
            return None
        return DrainFollower(self._drain_scale)

    def _observe_drain(self, follower: DrainFollower | None, rate: int) -> float | None:
        """Feed ``follower`` one reading of the sink's clock; returns the scale
        to resample at from here when it moved."""
        if follower is None:
            return None
        stats = cast("AudioStreamer", self._audio).stats()
        api = getattr(self._audio, "api", None)
        trust = (
            int(stats["full_underruns"]) + int(stats["partial_underruns"]),
            getattr(api, "delivery_epoch", 0),
        )
        before = follower.scale
        retuned = follower.observe(time.monotonic(), self._audio.position_seconds() or 0.0, trust)
        if retuned is not None:
            log.info(
                "audio file: the DAC drains at %.3f of real time (was following %.3f) — "
                "resampling %s to %d Hz so it plays at its own speed and pitch",
                retuned,
                before,
                os.path.basename(self._path),
                self._drained_rate(rate, retuned),
            )
        return retuned

    @staticmethod
    def _drained_rate(rate: int, scale: float) -> int:
        """The rate to resample a track to so a sink draining at ``scale`` of
        ``rate`` plays it in real time."""
        return max(1, int(round(rate * scale)))

    def _push_frame(self, resampled: Any) -> int:
        """Push one resampled frame to the sink; returns the samples it
        accepted.

        Not the samples handed over: the DAC drops a blob its queue held full
        past the put timeout, and a sampler that gave up on its link takes
        nothing. Counted, those put the length `finished` waits for past
        anything the sink's clock reaches, and the scene sat out the deadline
        on silence, the rest of the track when the sink died mid-file."""
        import numpy as np

        arr = resampled.to_ndarray().reshape(-1).astype(np.int16, copy=False)
        step = self._max_push_samples(self._audio.effective_rate or self._audio.sample_rate)
        accepted = 0
        for start in range(0, arr.size, step):
            if self._stop.is_set():
                break
            accepted += int(self._audio.push_samples(arr[start : start + step]))
        return accepted

    def _mark_decode_done(self, pushed_samples: int) -> None:
        """Record the end of decoding: the length of the audio pushed, on the
        sink's clock, and the deadline past which `finished` stops waiting for
        that clock (the audio unplayed now, plus `_DRAIN_GRACE_S`).

        Also tells the sink no more is coming: both wait for a prebuffer before
        they play, and a clip shorter than it never fills one."""
        self._audio.end_input()
        # The sink's clock divides by its effective rate, so the length does
        # too; the resampler's rounded integer rate would put it out of reach.
        rate = float(self._audio.effective_rate or self._audio.sample_rate)
        length = pushed_samples / rate if rate > 0 else 0.0
        played = self._audio.position_seconds() or 0.0
        lag = self._content_lag()
        played = min(max(played, 0.0), length + lag)
        self._end = (length, time.monotonic() + (length - played) + self._DRAIN_GRACE_S, lag)

    def _content_lag(self) -> float:
        """How far the sink plays its audio behind its clock: a sampler's
        re-anchors (`UltimateAudioSampler.content_lag_seconds`). The DAC's
        clock counts the samples that landed, so it has none."""
        return float(self._audio.content_lag_seconds)

    @property
    def finished(self) -> bool:
        """True once the decoder has ended (EOF or failure) and the sink's
        clock has reached the end of what it pushed, or the deadline has
        passed. False while decoding and before the first `setup()`.

        Read off the sink's clock at each call rather than scheduled when
        decoding ends: the sampler only starts its clock once its ring is
        gated, after the decoder may already have finished."""
        end = self._end
        if end is None:
            return False
        length, deadline, lag_at_end = end
        # A sampler that re-anchored late audio plays it that far behind its
        # clock; ended on the clock alone, the scene cut off the track's tail.
        # The bound waits it out too: re-anchors over a slow stretch add up,
        # and a lag past the grace ended the scene with the tail still queued.
        # But the lag gained after decoding ended counts only up to
        # `_MAX_COUNTED_LAG_S`: the lag grows with each re-anchor, and a
        # producer that keeps falling behind would otherwise never let the
        # bound arrive.
        lag = self._content_lag()
        if self._heard_seconds() >= length - 1e-3:
            return True
        counted = min(lag, lag_at_end + self._MAX_COUNTED_LAG_S)
        if time.monotonic() < deadline + counted:
            return False
        if lag > counted and not self._lag_cap_logged:
            self._lag_cap_logged = True
            log.warning(
                "audio file: ending the scene with the sink's content lag at %.1f s, "
                "%.1f s of it gained after decoding ended, past the %.0f s the end "
                "waits for; the re-anchored tail may be cut",
                lag,
                lag - lag_at_end,
                self._MAX_COUNTED_LAG_S,
            )
        return True

    def _heard_seconds(self) -> float:
        """`heard_seconds` of the sink. The analyzer's index and the end of
        track both read this, so the last lag's worth of a re-anchored track
        is not cut off."""
        return heard_seconds(self._audio)

    def teardown(self) -> None:
        # The sink is unhooked before the streamer stops, so no callback can
        # push into a dying tap.
        self._stop.set()
        self._audio.analysis_sink = None
        thread = self._thread
        features, self._features = self._features, None
        steps: list[tuple[str, Callable[[], object]]] = []
        if features is not None:
            steps.append(("feature stream stop", features.stop))
        steps.append(("audio stop", self._audio.stop))
        if thread is not None:
            steps.append(("decode thread join", partial(self._join_decode_thread, thread)))
        run_teardown_steps(log, type(self).__name__, steps)

    def _join_decode_thread(self, thread: threading.Thread) -> None:
        thread.join(2.0)
        if thread.is_alive():
            log.error("audio file: decode thread did not stop; keeping it fenced from restart")
        elif self._thread is thread:
            self._thread = None

    def position_seconds(self) -> float | None:
        # The consumer clock, for the protocol. The scene ends on `finished`.
        return self._audio.position_seconds()

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None


class SidFileAudioSource:
    """Plays a .sid file on the U64's real SID chip — the audio half of
    WaveformScene, factored out so a SourceScene can pair SID playback with any
    FrameSource (e.g. a generative plasma).

    Mechanism (identical to WaveformScene's audio path): DMA the SID payload +
    a tiny 6502 player into C64 RAM and kick a BASIC SYS stub, so the real 6510
    drives INIT + PLAY on a CIA #1 IRQ chained to kernal $EA31. The player owns
    the $0314 IRQ vector for PLAY.

    Two constraints versus WaveformScene, both because a SourceScene's display
    mode is hardwired to VIC bank 0 and cannot relocate:

    * **The SID payload must clear the display regions.** Char displays
      (petscii/mcm) reserve only screen RAM at $0400; bitmap displays also
      reserve the hires bitmap at $2000. A payload that overlaps either is
      refused (see payload_overlaps_bank0_display). Most HVSC tunes load at
      $1000 with multi-KB payloads, so bitmap+SID is frequently infeasible —
      char displays are the robust pairing.
    * **The display must NOT use the REU bank-swap.** That pipeline installs
      its own $0314 raster IRQ, which would collide with the SID player's PLAY
      IRQ. The config layer forces host-DMA (use_reu_staged=False) for any
      SID-audio scene; this class assumes that's been done.

    No DAC AudioStreamer is involved — the chip plays autonomously, regardless
    of [audio].enabled (like WaveformScene/MidiScene). `wants_audio_lock=True`
    so the SourceScene contends for the ensemble audio slot.

    Music-reactive visuals: when `reactive` (default True), setup() also spins up
    a host-side `SidFeatureStream` (a persistent SidHostEmu + poll thread that
    runs the same tune in parallel) and `features()` exposes its live
    `MusicModulation` snapshot, so a generative source can breathe with the tune.
    This is entirely host-side — it adds no U64 traffic. `reactive=False` (or a
    feature-stream startup failure) leaves features() returning None, so the
    visuals fall back to their pure time-driven behavior.
    """

    wants_audio_lock = True
    # run_sid_player goes through run_prg, which re-inits the machine to text
    # mode, so SourceScene.setup must re-assert the display mode afterwards.
    resets_display = True
    finished = False  # the chip plays on until teardown; duration_s ends it

    # Mirrors WaveformScene._pick_and_load_sid: a rejected SID is skipped.
    _MAX_PICK_ATTEMPTS = 8

    def __init__(
        self,
        api: C64Backend,
        file: str,
        *,
        song: int = 0,
        display_mode: DisplayMode,
        system: str = "NTSC",
        reactive: bool = True,
        sid_model: str = "auto",
        sid_panning: Sequence[int | str] | None = None,
        sid_volume: Sequence[int | str] | None = None,
        sid_play_rate: str | float | None = None,
    ):
        self._api = api
        self.file_spec = file
        self._song_arg = song
        self.system = system
        self._reactive = reactive
        # Already resolved to "auto"/"6581"/"8580"/"off" by the caller
        # (sid_autoconfig.resolve_sid_model_cfg).
        self._sid_model = sid_model
        # [ultimate64].sid_play_rate — see api.run_sid_player.
        self._sid_play_rate = sid_play_rate
        # [ultimate64].sid_panning — empty/None means the auto spread.
        self._sid_panning = list(sid_panning or ())
        # [ultimate64].sid_volume — empty/None means "0 dB for a source that
        # would otherwise be inaudible, leave a deliberate level alone".
        self._sid_volume = list(sid_volume or ())
        # The display is fixed at VIC bank 0, so only the bitmap flag matters
        # to the payload-clearance check ($2000 as well as $0400).
        self._is_bitmapped = bool(getattr(display_mode, "is_bitmapped", False))
        # Set by _pick_and_load, again at every setup() so a pool rotates.
        self._sid_file: str = ""
        self.sid_bytes: bytes = b""
        self.song: int = 0
        self.header: SidHeader | None = None
        self._features: SidFeatureStream | None = None
        # Model autoconfig + mixer originals, recorded by setup() so teardown
        # can restore them (sid_autoconfig.apply_sid_autoconfig).
        from c64cast.sid.sid_hw_config import SidHwSession

        self._sid_session = SidHwSession(api)
        # At build time, so a misconfigured single scene raises there
        # (parity with WaveformScene.__init__).
        self._pick_and_load()

    def _validate_candidate(
        self, path: str, budget: HostEmuBudget | None = None
    ) -> tuple[bytes, int, SidHeader]:
        """Load + header-parse + bank-0 payload-clearance + PLAY pre-flight for
        one .sid. Raises ValueError on any rejection; returns (sid_bytes,
        resolved_song, header) on success. Shared by __init__'s early check
        and setup()'s authoritative pick.

        `budget` is the pool walk's shared analysis budget — see _pick_and_load.
        The pre-flight is an INIT plus PREFLIGHT_TICKS PLAY passes, and the tune
        sets what those cost; per candidate, that was unbounded in seconds."""
        from c64cast.sid.sid_host_emu import (
            _sid_payload_extent,
            parse_sid_header,
            payload_overlaps_bank0_display,
            sid_play_preflight,
        )

        if not os.path.exists(path):
            raise ValueError(f"sid audio: file not found: {path}")
        with open(path, "rb") as f:
            sid_bytes = f.read()
        header = parse_sid_header(sid_bytes)
        if self._song_arg < 0 or (self._song_arg > header.num_songs and self._song_arg != 0):
            raise ValueError(
                f"sid audio: song {self._song_arg} out of range "
                f"0..{header.num_songs} for {os.path.basename(path)}"
            )
        song = self._song_arg if self._song_arg > 0 else header.start_song
        conflict = payload_overlaps_bank0_display(sid_bytes, is_bitmapped=self._is_bitmapped)
        if conflict is not None:
            lo, hi = conflict
            region = "hires bitmap" if lo == 0x2000 else "screen RAM"
            p_lo, p_hi = _sid_payload_extent(sid_bytes)
            raise ValueError(
                f"sid audio: {os.path.basename(path)} payload ${p_lo:04X}-${p_hi:04X} "
                f"overlaps the display's {region} (${lo:04X}-${hi:04X}). A SID "
                f"audio source can't relocate the bank-0 display; use a char "
                f"display (petscii/mcm — they reserve only $0400) or a SID that "
                f"loads above ${hi:04X}."
            )
        refusal = sid_play_preflight(sid_bytes, song=song, budget=budget)
        if refusal is not None:
            raise ValueError(f"sid audio: {os.path.basename(path)} {refusal}. Refused.")
        return sid_bytes, song, header

    def _pick_and_load(self) -> None:
        """Re-resolve the spec, shuffle, and load the first candidate that
        validates. Sets self._sid_file/sid_bytes/song/header. Raises if every
        attempt fails (mirrors WaveformScene._pick_and_load_sid)."""
        from c64cast.app.scene_factory import SID_EXTS, resolve_file_spec
        from c64cast.sid.sid_host_emu import HostEmuBudget

        # Same recursion into the default SID dir the factory's validate
        # pass used, so a setup() re-pick sees the same HVSC pool.
        candidates = resolve_file_spec(
            self.file_spec, SID_EXTS, label="sid audio", recurse_default_sid_dir=True
        )
        pool = list(candidates)
        random.shuffle(pool)
        # ONE budget for the whole walk: each candidate costs a host-emulated
        # INIT plus a 50-pass pre-flight, so a per-candidate bound bounds
        # nothing.
        budget = HostEmuBudget()
        last_error: Exception | None = None
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                sid_bytes, song, header = self._validate_candidate(path, budget)
            except ValueError as e:
                log.warning("sid audio: skipping %s: %s", os.path.basename(path), e)
                last_error = e
                continue
            self._sid_file = path
            self.sid_bytes = sid_bytes
            self.song = song
            self.header = header
            if len(candidates) > 1:
                log.info(
                    "sid audio: picked %s from %d candidates",
                    os.path.basename(path),
                    len(candidates),
                )
            return
        raise ValueError(
            f"sid audio: file spec {self.file_spec!r} resolved to "
            f"{len(candidates)} candidate(s) but none could be loaded; "
            f"last error: {last_error}"
        )

    def setup(self) -> None:
        """Re-pick from the (re-resolved) pool and start SID playback on the
        chip. Raises ValueError on a hard failure (every candidate rejected, or
        run_sid_player refuses the tune — RSID / load<$0820 / under KERNAL);
        SourceScene.setup converts that into an aborted scene so the playlist
        advances."""
        from c64cast.sid.sid_host_emu import HostEmuBudget, analyze_placement

        self._pick_and_load()
        # The player MC goes in RAM the tune never writes (its INIT+PLAY write
        # footprint) and clear of the bank-0 display. The DAC ring
        # ($4000-$5FFF, VIC bank 1) is unused by a SID source, so it is not
        # reserved. One budget spans both footprint runs and their INITs (see
        # sid_host_emu.ANALYSIS_BUDGET_S).
        placement = analyze_placement(
            self.sid_bytes,
            song=self.song,
            budget=HostEmuBudget(),
            what=f"sid audio: {os.path.basename(self._sid_file)} song {self.song}",
        )
        from c64cast.hw.c64 import SCREEN, VIC_BANK_0

        avoid = bytearray(placement.avoid)
        avoid[VIC_BANK_0.SCREEN : VIC_BANK_0.SCREEN + SCREEN.N_CELLS] = b"\x01" * SCREEN.N_CELLS
        if self._is_bitmapped:
            avoid[VIC_BANK_0.BITMAP : VIC_BANK_0.BITMAP + SCREEN.BITMAP_BYTES] = (
                b"\x01" * SCREEN.BITMAP_BYTES
            )
        # $36 (BASIC out) when this tune reads live song data from RAM under
        # BASIC ROM (e.g. Galway's Times of Lore at $B400); else None (let
        # run_sid_player's address heuristic decide). See _play_bank_for_footprints.
        play_bank = placement.play_bank
        log.info(
            "sid audio: %s #%d → run_sid_player (display %s, play_bank=%s)",
            os.path.basename(self._sid_file),
            self.song,
            "bitmap" if self._is_bitmapped else "char",
            f"${play_bank:02X}" if play_bank is not None else "auto",
        )
        # Before INIT runs, so its first writes land on the matched chip.
        # No-op on "off"/TeensyROM/already-matching; restored in teardown.
        assert self.header is not None  # set by _pick_and_load, called above
        from c64cast.sid.sid_autoconfig import apply_sid_autoconfig

        self._sid_session.fold(apply_sid_autoconfig(self._api, self.header, self._sid_model))
        self._apply_sid_mixer()
        # May raise (RSID / load<$0820 / under KERNAL); SourceScene.setup
        # aborts the scene cleanly on it.
        self._api.run_sid_player(
            self.sid_bytes,
            song=self.song,
            avoid=avoid,
            play_bank=play_bank,
            play_rate=self._sid_play_rate,
        )

        # A feature-stream startup failure degrades to non-reactive rather
        # than taking down playback.
        if self._reactive:
            from c64cast.scenes.music_features import SidFeatureStream

            try:
                self._features = SidFeatureStream(
                    self.sid_bytes, song=self.song, system=self.system
                )
                self._features.start()
            except Exception:
                log.exception(
                    "sid audio: feature stream failed to start — visuals will not "
                    "react to the music (playback continues)"
                )
                self._features = None

    def _apply_sid_mixer(self) -> None:
        """Pan the tune's SID chip(s) across the mixer's stereo field and
        make every source they play on audible ([ultimate64].sid_panning /
        sid_volume). This path does no U64 address routing, so the source
        playing each chip is whatever currently answers its address — but on
        the emulated-stereo-SID surface (U2+) a spare enabled side is still
        pointed at any uncovered chip address, or the chip has no route to
        that output at all, and each side that ends up snooping a tune chip is
        set to the model that chip asked for. Originals fold into the same
        snapshot teardown restores, and the settled state is logged so a chip
        that ends up muted or on the wrong model says so."""
        assert self.header is not None  # set by _pick_and_load, called by setup
        from c64cast.sid.emusid_mixer import apply_emusid_model, apply_emusid_routing
        from c64cast.sid.sid_autoconfig import required_models_for
        from c64cast.sid.sid_panning import apply_panning, sources_for_addresses
        from c64cast.sid.sid_resolved import log_resolved_audio
        from c64cast.sid.sid_volume import apply_volume

        addresses = self.header.sid_addresses
        required = required_models_for(self._sid_model, self.header.sid_models, len(addresses))
        self._sid_session.fold(apply_emusid_routing(self._api, addresses))
        self._sid_session.fold(apply_emusid_model(self._api, addresses, required))
        sources = sources_for_addresses(self._api, addresses)
        panning = apply_panning(self._api, sources, self._sid_panning)
        self._sid_session.fold(panning.originals)
        self._sid_session.fold(apply_volume(self._api, sources, self._sid_volume))
        log_resolved_audio(self._api, addresses, required)

    def teardown(self) -> None:
        """Stop the feature stream, then SID playback. SID order mirrors
        WaveformScene.teardown: unhook our $0314 IRQ first (so the next PLAY tick
        can't rewrite the SID between the volume-clear and the gate-clears),
        flush, silence every tune chip at the address it played, and only then
        restore the SID config — the restore may re-point a U2+ emulated SID at
        its home base, and a side moved home mid-note keeps ringing where no
        write can ever reach it (a machine reset does not clear the emulation's
        voice state — HW-verified). No VIC-bank restore: a SID source never
        moved the bank (the display owns bank 0 throughout), and nothing here
        suppresses the cursor blink: `suppress_cursor_blink()` was removed in
        #232 because poking BLNSW never held — the editor's input-wait loop
        overwrites that byte microseconds after the DMA lands — and the BASIC
        clear-and-loop PRG is what actually keeps the cursor off."""
        from c64cast.hw.c64 import SID
        from c64cast.sid.sidemu import SID_REG_COUNT

        features, self._features = self._features, None
        steps: list[tuple[str, Callable[[], object]]] = []
        if features is not None:
            steps.append(("feature stream stop", features.stop))  # host-side; no U64 I/O
        zeros = bytes(SID_REG_COUNT)
        steps += [
            ("kernal IRQ vector restore", self._api.restore_kernal_irq_vector),
            ("flush vector restore", self._api.flush),
        ]
        steps += [
            (f"silence SID at ${base:04X}", partial(self._api.write_regs, f"{base:04X}", *zeros))
            for base in (self.header.sid_addresses if self.header is not None else ())
            if base != SID.BASE
        ]
        steps += [
            ("primary SID silence", self._api.silence_sid),
            ("flush silence", self._api.flush),
            ("SID address config restore", self._sid_session.restore),
        ]
        run_teardown_steps(log, type(self).__name__, steps)

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None
