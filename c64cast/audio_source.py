"""Pluggable audio sources for composable scenes.

The "audio source" building block, parallel to FrameSource. A `SourceScene`
pairs a video source with one of these, so the visual and the sound are chosen
independently.

Today's implementations:
- `NullAudioSource` — silence.
- `MicAudioSource` — streaming sampled audio from the shared AudioStreamer's
  live mic path (the same path WebcamScene uses).

SID-file playback (`api.run_sid_player`, currently driven by WaveformScene) and
full-track sampled streaming (AVFileSource → AudioStreamer.push_samples) are
future implementations of this same protocol — that's the seam that will let
"generative video + SID audio" compose. `wants_audio_lock` lets a source that
drives the real SID (a future SidFileAudioSource) contend for the ensemble
audio slot; live/silent sources leave it False.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .config import AudioCfg
    from .modes import DisplayMode


@runtime_checkable
class AudioSource(Protocol):
    """How a SourceScene makes sound. `setup`/`teardown` bracket the scene;
    `position_seconds` exposes a master clock if the source owns one (None when
    it doesn't, e.g. a free-running mic)."""

    wants_audio_lock: bool

    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def position_seconds(self) -> float | None: ...


class NullAudioSource:
    """Silent. The default for a scene with no audio."""

    wants_audio_lock = False

    def setup(self) -> None:
        return None

    def teardown(self) -> None:
        return None

    def position_seconds(self) -> float | None:
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
