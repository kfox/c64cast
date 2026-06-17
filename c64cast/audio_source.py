"""Pluggable audio sources for composable scenes.

The "audio source" building block, parallel to FrameSource. A `SourceScene`
pairs a video source with one of these, so the visual and the sound are chosen
independently.

Today's implementations:
- `NullAudioSource` — silence.
- `MicAudioSource` — streaming sampled audio from the shared AudioStreamer's
  live mic path (the same path WebcamScene uses).
- `SidFileAudioSource` — plays a .sid file on the U64's real chip (the audio
  half of WaveformScene, factored out so it composes with any FrameSource).
  This is the seam that lets "generative video + SID audio" compose.
  `wants_audio_lock=True` because it drives the SID, so a SourceScene using it
  contends for the ensemble audio slot; live/silent sources leave it False.

Full-track sampled streaming (AVFileSource → AudioStreamer.push_samples) is a
future implementation of this same protocol.
"""

from __future__ import annotations

import logging
import os
import random
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .backend import C64Backend
    from .config import AudioCfg
    from .modes import DisplayMode
    from .modulation import MusicModulation
    from .music_features import SidFeatureStream

log = logging.getLogger(__name__)


@runtime_checkable
class AudioSource(Protocol):
    """How a SourceScene makes sound. `setup`/`teardown` bracket the scene;
    `position_seconds` exposes a master clock if the source owns one (None when
    it doesn't, e.g. a free-running mic); `features` exposes a live music-feature
    snapshot for reactive visuals (None when the source has no feature stream)."""

    wants_audio_lock: bool

    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def position_seconds(self) -> float | None: ...
    def features(self) -> MusicModulation | None: ...


class NullAudioSource:
    """Silent. The default for a scene with no audio."""

    wants_audio_lock = False

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
    dispatcher, the mic REU pump must skip its own IRQ hook."""

    # A live mic is uncorrelated input, not the ensemble's SID spotlight —
    # it never claims the audio lock (matches WebcamScene's WANTS_AUDIO_LOCK=False).
    wants_audio_lock = False

    def __init__(
        self,
        audio: AudioStreamer,
        audio_cfg: AudioCfg,
        display_mode: DisplayMode | None = None,
    ):
        self._audio = audio
        self._cfg = audio_cfg
        self._display_mode = display_mode

    def setup(self) -> None:
        skip_hook = bool(getattr(self._display_mode, "audio_reu_pump_active", False))
        self._audio.start_mic(
            self._cfg.device,
            self._cfg.mic_sensitivity,
            self._cfg.noise_gate,
            skip_irq_vector_hook=skip_hook,
        )

    def teardown(self) -> None:
        self._audio.stop()

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        # A live mic has no SID host-emulator to read features from; a future
        # audio-tap feature source could light this up.
        return None


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
    ):
        self._api = api
        self.file_spec = file
        self._song_arg = song
        self.system = system
        self._reactive = reactive
        # The display is fixed at VIC bank 0; only the bitmap flag matters for
        # the payload-clearance check (bitmap modes reserve $2000 as well as
        # $0400). Read once at construction — the mode never changes per scene.
        self._is_bitmapped = bool(getattr(display_mode, "is_bitmapped", False))
        # Per-pick state, set by _pick_and_load (in __init__ for early
        # validation, again at every setup() so a directory pool rotates).
        self._sid_file: str = ""
        self.sid_bytes: bytes = b""
        self.song: int = 0
        # Host-side music-feature stream (built per setup() once the tune is
        # picked + playing); None when not reactive or before setup.
        self._features: SidFeatureStream | None = None
        # Validate the spec + first candidate now so a misconfigured single
        # scene raises at build time (parity with WaveformScene.__init__).
        self._pick_and_load()

    # ---- SID selection / validation ----------------------------------------

    def _validate_candidate(self, path: str) -> tuple[bytes, int]:
        """Load + header-parse + bank-0 payload-clearance + PLAY pre-flight for
        one .sid. Raises ValueError on any rejection; returns (sid_bytes,
        resolved_song) on success. Shared by __init__'s early check and
        setup()'s authoritative pick."""
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
        return sid_bytes, song

    def _pick_and_load(self) -> None:
        """Re-resolve the spec, shuffle, and load the first candidate that
        validates. Sets self._sid_file/sid_bytes/song. Raises if every attempt
        fails (mirrors WaveformScene._pick_and_load_sid)."""
        from .config import SID_EXTS, resolve_file_spec

        candidates = resolve_file_spec(self.file_spec, SID_EXTS, label="sid audio")
        pool = list(candidates)
        random.shuffle(pool)
        last_error: Exception | None = None
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                sid_bytes, song = self._validate_candidate(path)
            except ValueError as e:
                log.warning("sid audio: skipping %s: %s", os.path.basename(path), e)
                last_error = e
                continue
            self._sid_file = path
            self.sid_bytes = sid_bytes
            self.song = song
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

    def position_seconds(self) -> float | None:
        return None

    def features(self) -> MusicModulation | None:
        return self._features.features() if self._features is not None else None
