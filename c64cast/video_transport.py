"""DJ transport state machine for VideoScene (MIDI live-tune Phases 2-4).

``VideoTransportControls`` owns everything that happens after the first
transport touch: the touched/paused flags, the wall- and audio-anchored
clocks, the A/B loop machine, the record border, and the per-video loop
preset store. ``scenes.VideoScene`` holds one as ``self.transport`` and
keeps the duck-typed ``transport_*`` methods as one-line delegators —
``transport.TransportSession`` getattr-probes those names on whatever scene
is current, so the *contract* stays on the scene while the state machine
lives here, beside `transport.py`.

The clock semantics (why the pre-touch read in ``touch()`` precedes the flag
flip, why resume splices before unmuting, the scaled/PTS-domain conversion
on the tempo path) are documented per-method and in
docs/architecture/scenes.md under "VideoScene's transport surface".
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Literal

from .transport import LoopPresetStore, timecode

if TYPE_CHECKING:
    from .scenes import VideoScene

log = logging.getLogger(__name__)

# C64 palette index painted to $D020 while a loop is armed (index 2 = red).
RECORD_BORDER_COLOR = 2


class VideoTransportControls:
    """Seek/pause/loop state for one VideoScene run.

    State fields are public: the scene resets them via ``reset()`` each
    setup, and the transport tests pin the machine's transitions directly.
    """

    def __init__(self, scene: VideoScene, *, loop_audio: str = "on") -> None:
        self._scene = scene
        # Audio resync policy once transport is touched (MIDI live-tune
        # Phase 4): "on" keeps audio playing and re-syncs it across every
        # seek/pause/loop splice; "mute" is the Phase-2 escape valve (mute +
        # wall clock for the rest of the run). Resolved to `resync` at touch
        # time — "on" degrades to the mute/wall path when the scene has no
        # audio (stream).
        self.loop_audio = loop_audio
        self.loop_store: LoopPresetStore | None = None
        self.reset()

    def reset(self) -> None:
        """Back to the untouched state — called at construction and from each
        ``VideoScene.setup()`` so a repeated/looped scene starts on the
        audio-master clock rather than inheriting a prior run's
        pause/seek/loop/mute."""
        self.touched = False
        self.paused = False
        self.wall_anchor_clock_s = 0.0
        self.wall_anchor_time = 0.0
        self.resync = False
        # Audio-anchored post-touch clock (resync path): playback clock =
        # audio_anchor_clock_s + (audio.position_seconds() - audio_anchor_pos),
        # frozen at audio_anchor_clock_s while paused. Re-anchored at
        # touch/pause/resume/seek. Lives in the scaled/PTS domain (see the
        # clock_to_content/content_to_clock helpers).
        self.audio_anchor_clock_s = 0.0
        self.audio_anchor_pos = 0.0
        self.loop_a: float | None = None
        self.loop_b: float | None = None
        self.loop_state: Literal["none", "armed", "active"] = "none"
        self.record_border_active = False

    def clock_to_content(self, clk: float) -> float:
        """Map an internal clock value (scaled/PTS domain) to content seconds.
        Identity except on the resync path over the DAC+bitmap tempo scale:
        there the clock advances at s×content-seconds, so divide by s to recover
        content seconds for the transport surface (seek targets, loop A/B, OSD)."""
        if self.touched and self.resync and self._scene.tempo_scale != 1.0:
            return clk / self._scene.tempo_scale
        return clk

    def content_to_clock(self, s: float) -> float:
        """Inverse of clock_to_content: content seconds → internal clock domain."""
        if self.touched and self.resync and self._scene.tempo_scale != 1.0:
            return s * self._scene.tempo_scale
        return s

    def clock_s(self) -> float:
        # Once transport is touched, the playback clock comes from a transport
        # anchor rather than the free-running audio-position clock.
        #  - Resync path (loop_audio="on" with audio): audio-anchored — the
        #    anchor plus the audio consumer's position delta, frozen while
        #    paused. This inherits the shipped pre-touch clock's drift behavior
        #    on every backend (crucially the ~0.88x drain rate on DAC+bitmap,
        #    where a wall clock would desync ~7 s/min). Lives in the scaled/PTS
        #    domain — see the conversion helpers.
        #  - Mute path (loop_audio="mute", or no audio): the Phase-2 wall-clock
        #    anchor, verbatim (audio is muted, so its position is meaningless).
        sc = self._scene
        if self.touched:
            if self.resync:
                assert sc.audio is not None
                if self.paused:
                    return self.audio_anchor_clock_s
                return self.audio_anchor_clock_s + (
                    sc.audio.position_seconds() - self.audio_anchor_pos
                )
            if self.paused:
                return self.wall_anchor_clock_s
            return self.wall_anchor_clock_s + (time.time() - self.wall_anchor_time)
        if sc.audio and sc.audio.sample_rate:
            return sc.audio.position_seconds()
        return time.time() - sc.wall_start_time

    def touch(self) -> None:
        """First call latches transport control for the rest of this scene's run
        and resolves the audio policy (loop_audio):
          - "on" with a live audio stream → resync path: keep audio playing,
            switch the clock to the audio-anchored delta, do NOT mute.
          - "mute" (or no audio / no audio stream) → the Phase-2 escape valve:
            freeze the wall-clock anchor and mute the source permanently.
        No-op on subsequent calls."""
        if self.touched:
            return
        sc = self._scene
        # Read the pre-touch clock BEFORE flipping the flag — clock_s() branches
        # on `touched`, so a read taken after the flip would return the anchor's
        # own not-yet-seeded default instead of the real position.
        clock_s = self.clock_s()
        self.touched = True
        self.resync = (
            self.loop_audio == "on"
            and sc.audio is not None
            and sc.source is not None
            and sc.source.a_stream is not None
            and not getattr(sc.audio, "use_reu_pump", False)
        )
        if self.resync:
            assert sc.audio is not None
            # Pre-touch clock == audio position in the scaled domain, so the
            # anchor delta starts at zero and playback continues seamlessly.
            self.audio_anchor_clock_s = clock_s
            self.audio_anchor_pos = sc.audio.position_seconds()
        else:
            self.wall_anchor_clock_s = clock_s
            self.wall_anchor_time = time.time()
            if sc.source is not None:
                sc.source.set_muted(True)

    def _splice(self, target_s: float) -> None:
        """Resync-path splice primitive (target_s in content seconds): re-anchor
        the audio clock to the target, arm the demuxer's stale-audio guard, then
        drop everything already queued. Order is load-bearing — request_seek sets
        the _emit_audio pending-seek guard live FIRST, then flush() drains; the
        flush epoch handles any pusher already blocked inside push_samples."""
        sc = self._scene
        assert sc.audio is not None and sc.source is not None
        self.audio_anchor_clock_s = self.content_to_clock(target_s)
        self.audio_anchor_pos = sc.audio.position_seconds()
        sc.source.request_seek(target_s)
        sc.audio.flush()

    def pause(self) -> None:
        sc = self._scene
        self.touch()
        if self.resync:
            # Freeze the audio-anchored clock at the current reading (BEFORE
            # setting `paused`, which changes clock_s's branch), mute output, and
            # ask the consumer to silence the ring fast (sampler: $DF21 volume 0;
            # DAC: worker ring stomp). flush() drops queued audio so resume
            # starts clean.
            assert sc.audio is not None and sc.source is not None
            self.audio_anchor_clock_s = self.clock_s()
            self.paused = True
            sc.source.set_muted(True)
            sc.audio.flush(silence_output=True)
        else:
            self.wall_anchor_clock_s = self.clock_s()
            self.paused = True
        sc.osd.post("PAUSED")

    def resume(self) -> None:
        sc = self._scene
        if not self.paused:
            return
        if self.resync:
            # Splice back to the paused position first (re-anchors + flushes +
            # restores the sampler's volume via the plain flush()), THEN unmute —
            # this ordering closes the resume audio-leak window. During pause the
            # sampler's wall position kept advancing; the fresh audio_anchor_pos
            # in _splice absorbs it (the DAC's position froze on its own).
            assert sc.source is not None
            self.paused = False
            self._splice(self.clock_to_content(self.audio_anchor_clock_s))
            sc.source.set_muted(False)
        else:
            self.paused = False
            self.wall_anchor_time = time.time()
        sc.osd.post("PLAY")

    def toggle_pause(self) -> None:
        if not self.touched:
            self.pause()
        elif self.paused:
            self.resume()
        else:
            self.pause()

    def seek(self, target_s: float) -> None:
        sc = self._scene
        self.touch()
        # Clamp against the duration in CONTENT seconds (duration() is the file
        # duration, unscaled) — target_s is a content-seconds position.
        duration = self.duration()
        hi = duration if duration is not None else max(target_s, 0.0)
        target_s = max(0.0, min(target_s, hi))
        if self.resync:
            self._splice(target_s)
        else:
            self.wall_anchor_clock_s = target_s
            self.wall_anchor_time = time.time()
            if sc.source is not None:
                sc.source.request_seek(target_s)
        sc.osd.post(f"SEEK {timecode(target_s)}")

    def loop_toggle(self) -> None:
        """3-state cycle: mark A -> mark B + start looping -> clear. Drives
        the same loop_a/loop_b/loop_state machine as the Record/Stop pair
        (record()/stop()) — the red border/pad-slot persistence added there
        (MIDI live-tune Phase 3) apply here too, so the single-button and
        Record/Stop workflows give identical feedback."""
        sc = self._scene
        self.touch()
        pos = self.position()
        if self.loop_state == "none":
            self.loop_a = pos
            self.loop_b = None
            self.loop_state = "armed"
            self.set_record_border(True)
            sc.osd.post(f"LOOP A {timecode(pos)}")
        elif self.loop_state == "armed":
            self.loop_b = pos
            self.loop_state = "active"
            self.set_record_border(False)
            assert self.loop_a is not None
            sc.osd.post(f"LOOP {timecode(self.loop_a)}-{timecode(pos)}")
        else:
            self.loop_a = None
            self.loop_b = None
            self.loop_state = "none"
            sc.osd.post("LOOP OFF")

    def set_record_border(self, active: bool) -> None:
        """Red border while a loop is armed (MIDI live-tune Phase 3). The
        bitmap/char display modes VideoScene uses engage with a hardcoded
        black ($00) border and never rewrite $D020 per frame afterward (see
        modes.engage_bitmap_mode's docstring), so 0 is always the correct
        value to restore to — no per-mode border state to preserve."""
        if active == self.record_border_active:
            return
        self.record_border_active = active
        self._scene.api.write_regs("d020", RECORD_BORDER_COLOR if active else 0)

    def record(self) -> None:
        """Record button: arm a loop at the current position (first step of
        the Record -> Stop workflow; see stop()). A no-op beyond the usual
        transport touch if a loop is already armed or active — Stop governs
        every subsequent transition."""
        self.touch()
        if self.loop_state != "none":
            return
        pos = self.position()
        self.loop_a = pos
        self.loop_b = None
        self.loop_state = "armed"
        self.set_record_border(True)
        self._scene.osd.post(f"REC ● {timecode(pos)}")

    def stop(self) -> bool:
        """Stop button: context-sensitive 3-way action.

        - Recording (loop armed): close B, start looping.
        - Playing (not paused, looping or not): pause in place.
        - Already paused: request a full app exit — returns True, and the
          caller (TransportSession._dispatch) sets Playlist.stop_event.

        Held simultaneously with a loop_slot pad press, this SAVES the
        current loop into that slot (see loop_slot) — the plain press here
        still fires its own action first; a performer holds Stop a beat
        longer to reach the pad."""
        self.touch()
        if self.loop_state == "armed":
            assert self.loop_a is not None
            pos = self.position()
            self.loop_b = pos
            self.loop_state = "active"
            self.set_record_border(False)
            self._scene.osd.post(f"LOOP {timecode(self.loop_a)}-{timecode(pos)}")
            return False
        if not self.paused:
            self.pause()
            return False
        return True

    def loop_slot(self, slot: int, *, save: bool, clear: bool) -> None:
        """Pad press. `save`/`clear` are the Stop-held/Record-held chord
        flags TransportSession resolves before calling this — mutually
        exclusive, both False on a plain press (recall)."""
        sc = self._scene
        if clear:
            if self.loop_store is not None:
                self.loop_store.delete(slot)
            sc.osd.post(f"{slot} CLEARED")
            return
        if save:
            if self.loop_a is None:
                sc.osd.post("NO LOOP")
                return
            if self.loop_store is not None:
                self.loop_store.save(slot, self.loop_a, self.loop_b)
            sc.osd.post(f"SAVED {slot}")
            return
        entry = self.loop_store.load().get(str(slot)) if self.loop_store is not None else None
        if entry is not None:
            a = entry["a"]
            assert a is not None, "a stored loop entry always has a non-null 'a'"
            b = entry["b"]
        else:
            a, b = 0.0, None
        self.loop_a = a
        self.loop_b = b
        self.loop_state = "active"
        self.set_record_border(False)
        if self.paused:
            self.resume()
        self.seek(a)
        sc.osd.post(f"LOOP {slot}")

    def position(self) -> float:
        # The transport surface speaks content seconds; the internal clock is in
        # the scaled/PTS domain on the resync tempo path (identity elsewhere).
        return self.clock_to_content(self.clock_s())

    def duration(self) -> float | None:
        source = self._scene.source
        return source.duration_s if source is not None else None

    def is_paused(self) -> bool:
        return self.paused
