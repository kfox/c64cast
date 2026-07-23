"""Pluggable audio sources for composable scenes.

The "audio source" building block, parallel to FrameSource. A `SourceScene`
pairs a video source with one of these, so the visual and the sound are chosen
independently.

Today's implementations:
- `NullAudioSource` — silence.
- `MicAudioSource` — streaming sampled audio from the shared AudioStreamer's
  live mic path (the same path WebcamScene uses). Reactive by default: it also
  runs the pre-DSP audio analyzer, so live input drives the visuals. With
  `listen_only` (audio_source = "listen") it analyzes the input for reactive
  visuals but plays no C64 audio — the VJ case where the sound is on a PA.
- `AudioFileSource` — decodes an audio file (mp3/wav/… via PyAV) to the 4-bit
  DAC AND runs the same pre-DSP analyzer over it, so a generative/test-pattern
  visual reacts to the track. This is what makes `c64cast tune.mp3` a
  first-class reactive source (audio_source = "file"). It is the "full-track
  sampled streaming" implementation the protocol always anticipated.
- `SidFileAudioSource` — plays a .sid file on the U64's real chip (the audio
  half of WaveformScene, factored out so it composes with any FrameSource).
  This is the seam that lets "generative video + SID audio" compose.
  `wants_audio_lock=True` because it drives the SID, so a SourceScene using it
  contends for the ensemble audio slot; live/silent sources leave it False.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .audio_features import AudioFeatureStream
    from .backend import C64Backend
    from .config import AudioCfg, AudioFeaturesCfg
    from .modes import DisplayMode
    from .modulation import MusicModulation
    from .music_features import SidFeatureStream
    from .sid_host_emu import SidHeader

log = logging.getLogger(__name__)


@runtime_checkable
class AudioSource(Protocol):
    """How a SourceScene makes sound. `setup`/`teardown` bracket the scene;
    `position_seconds` exposes a master clock if the source owns one (None when
    it doesn't, e.g. a free-running mic); `features` exposes a live music-feature
    snapshot for reactive visuals (None when the source has no feature stream).

    `resets_display` is True when `setup()` disturbs the VIC display state — a
    SID source kicks its player via the firmware's run_prg, which re-inits the
    machine back to text mode. SourceScene re-asserts the display mode AFTER
    such a source starts so a bitmap display isn't left rendering text."""

    wants_audio_lock: bool
    resets_display: bool

    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def position_seconds(self) -> float | None: ...
    def features(self) -> MusicModulation | None: ...


class NullAudioSource:
    """Silent. The default for a scene with no audio."""

    wants_audio_lock = False
    resets_display = False

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

    # A live mic is uncorrelated input, not the ensemble's SID spotlight —
    # it never claims the audio lock (matches WebcamScene's WANTS_AUDIO_LOCK=False).
    wants_audio_lock = False
    resets_display = False  # the mic path doesn't touch the VIC

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
        # Built per setup() when reactive; None otherwise.
        self._features: AudioFeatureStream | None = None

    def setup(self) -> None:
        skip_hook = bool(getattr(self._display_mode, "audio_reu_pump_active", False))
        from .config import AudioFeaturesCfg

        fcfg = self._features_cfg or AudioFeaturesCfg()
        # Listen-only frees the capture from the DAC rate — open (and analyze)
        # at the higher listen rate. The mic path stays at the streamer's DAC
        # rate so the analyzer matches what the DAC actually samples.
        analyzer_rate = (
            float(fcfg.listen_sample_rate) if self._listen_only else self._audio.sample_rate
        )
        # Install the analysis tap BEFORE capture starts, so the first callbacks
        # already feed the analyzer.
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
        if self._features is not None:
            self._features.stop()
            self._features = None
        self._audio.stop()

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None


class AudioFileSource:
    """Decode an audio file (mp3/wav/flac/… via PyAV) to the 4-bit DAC and run
    the pre-DSP analyzer over it, so a generative/test-pattern visual reacts to
    the track. The audio half of `c64cast tune.mp3` (audio_source = "file").

    Mechanism: a background thread demuxes + resamples the file to the streamer's
    mono int16 rate and feeds `AudioStreamer.push_samples`, exactly as
    `AVFileSource` feeds a video's audio — push_samples both DAC-encodes the
    samples and (pre-DSP) forwards them to `analysis_sink`, so the *same*
    analyzer the mic path uses drives the visuals off the decoded audio. Playback
    is real-time paced by push_samples' backpressure (queue-full block), so the
    decode thread naturally tracks the DAC consumption rate.

    `wants_audio_lock=False`: like the mic/video paths, a file is not the
    ensemble's SID spotlight (`config.build_scene` also suppresses its DAC audio
    in ensemble mode). `resets_display=False`: the DAC path never touches the VIC.

    `duration_s` (read from the container at construction) lets `build_scene` size
    the scene to the track so `c64cast tune.mp3` plays the whole song then
    advances/loops. A startup failure degrades to non-reactive silence with the
    scene intact, the same contract as `MicAudioSource`/`SidFileAudioSource`.
    """

    wants_audio_lock = False
    resets_display = False

    # Bounded candidate attempts for a multi-entry (dir/glob) spec, mirroring
    # SidFileAudioSource: a file that won't open is skipped and the next tried.
    _MAX_PICK_ATTEMPTS = 8

    def __init__(
        self,
        audio: AudioStreamer,
        file: str,
        *,
        reactive: bool = True,
        features_cfg: AudioFeaturesCfg | None = None,
    ):
        self._audio = audio
        self.file_spec = file
        self._reactive = reactive
        self._features_cfg = features_cfg
        self._path: str = ""
        # Track length in seconds (from the container), used by build_scene to
        # size the scene. 0.0 when the container reports no duration.
        self.duration_s: float = 0.0
        self._features: AudioFeatureStream | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Validate + measure the first candidate now, so a misconfigured single
        # scene raises at build time (parity with SidFileAudioSource.__init__).
        self._pick_and_probe()

    # ---- file selection / probe --------------------------------------------

    def _pick_and_probe(self) -> None:
        """Re-resolve the spec, shuffle, and probe the first file that opens.
        Sets self._path + self.duration_s. Raises if none open."""
        from .config import AUDIO_EXTS, resolve_file_spec
        from .video import _av_open, _ensure_pyav

        if not _ensure_pyav():
            raise RuntimeError("PyAV not installed; install with `pip install c64cast[video]`")
        candidates = resolve_file_spec(self.file_spec, AUDIO_EXTS, label="audio file")
        pool = list(candidates)
        random.shuffle(pool)
        last_error: Exception | None = None
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                container = _av_open(path)
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

    # ---- AudioSource protocol ----------------------------------------------

    def setup(self) -> None:
        """Re-pick from the (re-resolved) pool, install the analyzer, and spin up
        the decode→DAC thread. Never raises on a decode/analyzer hiccup — degrades
        to non-reactive so the visual keeps running."""
        self._pick_and_probe()
        self._stop.clear()
        self._start_features()
        self._audio.start_for_external_source()
        thread = threading.Thread(target=self._decode_loop, daemon=True, name="audio-file-decode")
        self._thread = thread
        thread.start()
        log.info(
            "audio file: %s → DAC @ %dHz%s",
            os.path.basename(self._path),
            self._audio.sample_rate,
            " (reactive)" if self._features is not None else "",
        )

    def _start_features(self) -> None:
        """Install the pre-DSP analyzer at the streamer's DAC rate (what the DAC
        actually plays, like the mic path). A failure must not cost playback."""
        if not self._reactive:
            return
        from .audio_features import AnalysisTap, AudioFeatureStream
        from .config import AudioFeaturesCfg

        cfg = self._features_cfg or AudioFeaturesCfg()
        try:
            tap = AnalysisTap(size=max(cfg.fft_size * 4, 4096))
            stream = AudioFeatureStream(
                tap,
                self._audio.sample_rate,
                n_bands=cfg.bands,
                fft_size=cfg.fft_size,
                poll_hz=cfg.poll_hz,
                onset_sensitivity=cfg.onset_sensitivity,
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

    def _decode_loop(self) -> None:
        """Demux + resample the file to mono int16 at the DAC rate and feed
        push_samples (which DAC-encodes AND taps the analyzer). Real-time paced by
        push_samples' queue-full block. Ends at EOF or when `_stop` is set."""
        import numpy as np

        from .video import _av_open

        try:
            container = _av_open(self._path)
        except Exception:
            log.exception("audio file: could not open %s for decode", self._path)
            return
        try:
            import av  # noqa: PLC0415  (optional extra; only reached when PyAV present)

            resampler = av.AudioResampler(format="s16", layout="mono", rate=self._audio.sample_rate)
            a_stream = container.streams.audio[0]
            for packet in container.demux(a_stream):
                if self._stop.is_set():
                    return
                for frame in packet.decode():
                    for resampled in resampler.resample(frame):
                        if self._stop.is_set():
                            return
                        arr = resampled.to_ndarray().reshape(-1).astype(np.int16, copy=False)
                        if arr.size:
                            self._audio.push_samples(arr)
            log.info("audio file: %s reached end of track", os.path.basename(self._path))
        except Exception:
            if not self._stop.is_set():
                log.exception("audio file: decode of %s failed", os.path.basename(self._path))
        finally:
            container.close()

    def teardown(self) -> None:
        # Signal the decode thread, unhook the analyzer sink before the streamer
        # stops (so no callback pushes into a dying tap), then stop everything.
        self._stop.set()
        self._audio.analysis_sink = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._features is not None:
            self._features.stop()
            self._features = None
        self._audio.stop()

    def position_seconds(self) -> float | None:
        # The DAC consumer clock — exposed for the protocol; the scene ends on its
        # duration (sized to the track), not this.
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
    # run_sid_player kicks the player via the firmware's run_prg, which re-inits
    # the machine to text mode — so SourceScene must re-assert the display mode
    # after setup() (a bitmap display would otherwise render its $0400 colour
    # nibbles as PETSCII; see SourceScene.setup).
    resets_display = True

    # Bounded candidate attempts for a multi-entry file spec, mirroring
    # WaveformScene._pick_and_load_sid: each rejected SID (payload overlap,
    # raster-spin preflight) is skipped and the next tried.
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
    ):
        self._api = api
        self.file_spec = file
        self._song_arg = song
        self.system = system
        self._reactive = reactive
        # [ultimate64].sid_model, already resolved to a plain string by the
        # caller (sid_autoconfig.resolve_sid_model_cfg). "auto"/"6581"/
        # "8580"/"off". See setup()/teardown().
        self._sid_model = sid_model
        # The display is fixed at VIC bank 0; only the bitmap flag matters for
        # the payload-clearance check (bitmap modes reserve $2000 as well as
        # $0400). Read once at construction — the mode never changes per scene.
        self._is_bitmapped = bool(getattr(display_mode, "is_bitmapped", False))
        # Per-pick state, set by _pick_and_load (in __init__ for early
        # validation, again at every setup() so a directory pool rotates).
        self._sid_file: str = ""
        self.sid_bytes: bytes = b""
        self.song: int = 0
        self.header: SidHeader | None = None
        # Host-side music-feature stream (built per setup() once the tune is
        # picked + playing); None when not reactive or before setup.
        self._features: SidFeatureStream | None = None
        # SID hardware config (model autoconfig) snapshotted by setup() so
        # teardown can restore it. None means "nothing applied, nothing to
        # restore" — see sid_autoconfig.apply_sid_autoconfig.
        self._saved_sid_config: dict[tuple[str, str], str] | None = None
        # Validate the spec + first candidate now so a misconfigured single
        # scene raises at build time (parity with WaveformScene.__init__).
        self._pick_and_load()

    # ---- SID selection / validation ----------------------------------------

    def _validate_candidate(self, path: str) -> tuple[bytes, int, SidHeader]:
        """Load + header-parse + bank-0 payload-clearance + PLAY pre-flight for
        one .sid. Raises ValueError on any rejection; returns (sid_bytes,
        resolved_song, header) on success. Shared by __init__'s early check
        and setup()'s authoritative pick."""
        from .sid_host_emu import (
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
        if not sid_play_preflight(sid_bytes, song=song):
            raise ValueError(
                f"sid audio: {os.path.basename(path)} PLAY never completes within "
                f"the host emulator's cycle cap — the tune spins on a raster/IRQ "
                f"the player environment doesn't provide; it would hang the "
                f"C64-side player (silent + unresponsive). Refused."
            )
        return sid_bytes, song, header

    def _pick_and_load(self) -> None:
        """Re-resolve the spec, shuffle, and load the first candidate that
        validates. Sets self._sid_file/sid_bytes/song/header. Raises if every
        attempt fails (mirrors WaveformScene._pick_and_load_sid)."""
        from .config import SID_EXTS, resolve_file_spec

        candidates = resolve_file_spec(self.file_spec, SID_EXTS, label="sid audio")
        pool = list(candidates)
        random.shuffle(pool)
        last_error: Exception | None = None
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                sid_bytes, song, header = self._validate_candidate(path)
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

    # ---- AudioSource protocol ----------------------------------------------

    def setup(self) -> None:
        """Re-pick from the (re-resolved) pool and start SID playback on the
        chip. Raises ValueError on a hard failure (every candidate rejected, or
        run_sid_player refuses the tune — RSID / load<$0820 / under KERNAL);
        SourceScene.setup converts that into an aborted scene so the playlist
        advances."""
        from .sid_host_emu import (
            _play_bank_for_footprints,
            ram_play_access_footprint,
            ram_write_footprint,
        )

        self._pick_and_load()
        # The player MC must be relocated into RAM the tune never writes (its
        # INIT+PLAY *write* footprint) AND clear of the bank-0 display we're
        # rendering into. The audio DAC ring ($4000-$5FFF, VIC bank 1) is NOT
        # used by a SID source, so it isn't reserved — the payload may freely
        # live there.
        footprint = ram_write_footprint(self.sid_bytes, song=self.song)
        from .c64 import SCREEN, VIC_BANK_0

        avoid = bytearray(footprint)
        avoid[VIC_BANK_0.SCREEN : VIC_BANK_0.SCREEN + SCREEN.N_CELLS] = b"\x01" * SCREEN.N_CELLS
        if self._is_bitmapped:
            avoid[VIC_BANK_0.BITMAP : VIC_BANK_0.BITMAP + SCREEN.BITMAP_BYTES] = (
                b"\x01" * SCREEN.BITMAP_BYTES
            )
        # $36 (BASIC out) when this tune reads live song data from RAM under
        # BASIC ROM (e.g. Galway's Times of Lore at $B400); else None (let
        # run_sid_player's address heuristic decide). See _play_bank_for_footprints.
        access_fp = ram_play_access_footprint(self.sid_bytes, song=self.song)
        play_bank = _play_bank_for_footprints(footprint, access_fp)
        log.info(
            "sid audio: %s #%d → run_sid_player (display %s, play_bank=%s)",
            os.path.basename(self._sid_file),
            self.song,
            "bitmap" if self._is_bitmapped else "char",
            f"${play_bank:02X}" if play_bank is not None else "auto",
        )
        # Match the tune's requested chip model (PSID header) to the U64's
        # actual SID hardware BEFORE the player's INIT runs, so INIT's first
        # writes land on the matched chip. No-op on "off"/TeensyROM/already-
        # matching. Snapshot restored in teardown.
        assert self.header is not None  # set by _pick_and_load, called above
        from .sid_autoconfig import apply_sid_autoconfig

        self._saved_sid_config = apply_sid_autoconfig(self._api, self.header, self._sid_model)
        # May raise (RSID / load<$0820 / under KERNAL) — propagate to
        # SourceScene.setup, which aborts the scene cleanly.
        self._api.run_sid_player(self.sid_bytes, song=self.song, avoid=avoid, play_bank=play_bank)

        # Spin up the host-side feature stream for reactive visuals. The tune
        # already passed run_sid_player (and the host-emu preflight in
        # _validate_candidate), so this shouldn't fail — but a startup failure
        # must not take down playback, so degrade to non-reactive on error.
        if self._reactive:
            from .music_features import SidFeatureStream

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

    def teardown(self) -> None:
        """Stop the feature stream, then SID playback. SID order mirrors
        WaveformScene.teardown: unhook our $0314 IRQ first (so the next PLAY tick
        can't rewrite the SID between the volume-clear and the gate-clears),
        flush, then silence. Finally suppress the cursor blink — the player MC's
        `JMP *` spin survives teardown, so a following char scene would otherwise
        blink the cursor cell (HW-verified in WaveformScene.teardown). No
        VIC-bank restore: a SID source never moved the bank (the display owns
        bank 0 throughout)."""
        if self._features is not None:
            self._features.stop()  # pure host-side; no U64 I/O
            self._features = None
        try:
            self._api.restore_kernal_irq_vector()
            self._api.flush()
            self._api.silence_sid()
            self._api.flush()
            self._api.suppress_cursor_blink()
            self._api.flush()
        except Exception:
            log.exception("sid audio: teardown silence/restore failed")
        if self._saved_sid_config:
            from .sid_hw_config import restore_sid_config

            restore_sid_config(self._api, self._saved_sid_config)
            self._saved_sid_config = None

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None
