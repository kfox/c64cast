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

from c64cast.audio.audio_source import heard_seconds
from c64cast.control.transport import LoopPresetStore, timecode
from c64cast.hw.c64 import RegionID

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
        # "on" keeps audio playing and re-syncs it across every splice; "mute"
        # mutes and runs on the wall clock for the rest of the run. Resolved to
        # `resync` at touch time, where "on" degrades to mute without an audio
        # stream.
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
        # True until a seek is requested: the clock is still the PTS timeline
        # the source rebased to 0 at start_s, which a touch alone does not
        # change. A pause or a loop mark between the touch and the first seek
        # reads it, so the offset back to a file position goes with the
        # rebase rather than with the touch.
        self.rebased = True
        # The resync path's post-touch clock, in the scaled/PTS domain:
        # clock + (heard_seconds(audio) - pos) for an anchor (clock, pos),
        # held at clock while paused or while pos is None (a splice waiting on
        # its flush), re-anchored at touch/pause/resume/seek. One tuple, so
        # the web console's HTTP worker, which reads position() off the
        # playlist thread, never pairs one anchor's clock with another's pos.
        self.audio_anchor: tuple[float, float | None] = (0.0, 0.0)
        self.loop_a: float | None = None
        self.loop_b: float | None = None
        self.loop_state: Literal["none", "armed", "active"] = "none"
        self.record_border_active = False

    @property
    def audio_anchor_clock_s(self) -> float:
        return self.audio_anchor[0]

    @property
    def audio_anchor_pos(self) -> float | None:
        return self.audio_anchor[1]

    def clock_to_content(self, clk: float) -> float:
        """Map an internal clock value (scaled/PTS domain) to content seconds.
        After the first seek it is the identity except on the resync path over
        the DAC+bitmap tempo scale: there the clock advances at
        s×content-seconds, so invert the source's tempo map (offset + c×s once
        a retune has run) to recover content seconds for the
        transport surface (seek targets, loop A/B, OSD).

        Before the touch the clock is the PTS timeline the source rebased to 0
        at start_s and scaled by the tempo, so it is unscaled and offset back
        to a file position: a jog or the web console reads ``position()``
        here, before anything has touched transport. The start_s offset
        stays until the first seek (`rebased`), which re-anchors the clock to
        an absolute file position."""
        sc = self._scene
        if self._clock_scaled():
            # The source's own map once there is one: a retune of the tempo
            # (VideoScene._follow_drain) moves it off a plain ratio.
            clk = sc.source.clock_to_content(clk) if sc.source else clk / (sc.tempo_scale or 1.0)
        return clk + (sc.start_s if self.rebased else 0.0)

    def content_to_clock(self, s: float) -> float:
        """Inverse of clock_to_content: content seconds → internal clock domain."""
        sc = self._scene
        s -= sc.start_s if self.rebased else 0.0
        if not self._clock_scaled():
            return s
        return sc.source.content_to_clock(s) if sc.source else s * (sc.tempo_scale or 1.0)

    def _clock_scaled(self) -> bool:
        """Whether the clock runs at tempo_scale x content seconds: the
        audio clock before the touch, and the resync path's after it."""
        return not self.touched or (self.resync and self._scene.tempo_scale != 1.0)

    def clock_s(self) -> float:
        """The playback clock: the free-running audio position — or the wall
        clock when the scene has no audio stream — until transport is touched,
        and a transport anchor after.

        The resync path anchors to the audio consumer's position delta, which
        inherits its drift behavior on every backend — on DAC+bitmap the drain
        runs ≈0.88× wall, where a wall clock would desync ≈7 s/min. The mute
        path anchors to the wall clock instead, audio being muted there and its
        position meaningless.
        """
        sc = self._scene
        if self.touched:
            if self.resync:
                return self._resync_clock_s(self.audio_anchor)
            if self.paused:
                return self.wall_anchor_clock_s
            return self.wall_anchor_clock_s + (time.time() - self.wall_anchor_time)
        if sc.audio and sc.audio.sample_rate:
            # The heard position, not the sink's raw clock: a sampler that
            # re-anchored late audio plays it that far behind its clock, and
            # the picture read off the raw clock ran that far ahead of the
            # sound and ended with the last lag's worth of audio unshown.
            return heard_seconds(sc.audio)
        return time.time() - sc.wall_start_time

    def _resync_clock_s(self, anchor: tuple[float, float | None]) -> float:
        """clock_s() on the resync path, from one read of the anchor."""
        sc = self._scene
        assert sc.audio is not None
        clock, pos = anchor
        if self.paused or pos is None:
            return clock
        return clock + (heard_seconds(sc.audio) - pos)

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
        sc.release_audio_hold()
        if sc.source is not None:
            sc.source.freeze_tempo()
        # BEFORE the flag flip: clock_s() branches on `touched`, so a read taken
        # after it returns the anchor's own unseeded default.
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
            # The pre-touch clock is the audio position in the scaled domain, so
            # the anchor delta starts at zero and playback carries on unbroken.
            self.audio_anchor = (clock_s, heard_seconds(sc.audio))
        else:
            self.wall_anchor_clock_s = clock_s
            self.wall_anchor_time = time.time()
            if sc.source is not None:
                sc.source.set_muted(True)

    def _splice(self, target_s: float, *, unmute: bool = False) -> None:
        """Resync-path splice primitive (target_s in content seconds): re-anchor
        the audio clock to the target, arm the demuxer's stale-audio guard, and
        retire everything pushed before it. Order is load-bearing:
        request_seek sets the _emit_audio pending-seek guard and takes the
        sink's cut (its flush-epoch bump and anchor) in one critical section,
        then flush() finishes the cut (the sampler's ring cut-over, the DAC's
        stomp request). Both sinks drop pre-cut audio by its epoch tag
        wherever it is, and keep the target's audio the demuxer pushes
        between the cut and the flush.

        ``unmute`` (resume) unlatches the source in that same critical
        section. The demuxer can apply the seek and decode the target's first
        audio while flush() runs (the sampler's cut-over waits on the ring
        writer and blanks the old lead, tens of ms), and a source still muted
        then drops it: the stream starts that much past the target at the
        anchor, and the sound plays ahead of the picture."""
        sc = self._scene
        assert sc.audio is not None and sc.source is not None
        # The anchor's pos is what the cut returns: where the target's
        # first sample is heard, one ring lead from now, read once on the
        # clock as it runs after the flush, which clears a sampler's
        # end-of-stream clamp. Until then the clock holds at the target: an
        # estimate read before the flush paired a clamped position with the
        # unclamped clock the flush leaves, and the web console's poll read
        # the target plus the clamp's overrun.
        self.rebased = False
        clock = self.content_to_clock(target_s)
        self.audio_anchor = (clock, None)
        try:
            cut = sc.source.request_seek(target_s, unmute=unmute, on_request=sc.audio.cut)
            pos = sc.audio.flush(cut=cut)
        except BaseException:
            # Held at the target for good otherwise: run on from where the
            # sink's clock says the ring's last sample is heard.
            self.audio_anchor = (clock, sc.audio.position_seconds() + sc.audio.ring_lead_seconds())
            raise
        self.audio_anchor = (clock, pos)

    def pause(self) -> None:
        sc = self._scene
        self.touch()
        if self.resync:
            # Freeze the clock BEFORE setting `paused`, which changes clock_s's
            # branch. The silencing flush is the fast one (sampler: $DF21 volume
            # 0; DAC: worker ring stomp) and drops queued audio, so resume
            # starts clean.
            # Frozen at the splice target if one is still being waited out, or
            # a pause inside the hold would resume a ring lead short of it.
            assert sc.audio is not None and sc.source is not None
            # Held by the None pos as well as the flag, so a poll between the
            # two stores reads the frozen clock.
            self.audio_anchor = (self.target_clock_s(), None)
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
            # Splice back to the paused position, unmuting the source as the
            # seek is requested (see _splice). The
            # sampler's wall position kept advancing through the pause, and the
            # fresh audio_anchor_pos absorbs it (the DAC's position froze on
            # its own).
            assert sc.source is not None
            self._splice(self.clock_to_content(self.audio_anchor_clock_s), unmute=True)
            self.paused = False
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
        # Both target_s and duration() are content seconds, unscaled.
        duration = self.duration()
        hi = duration if duration is not None else max(target_s, 0.0)
        target_s = max(0.0, min(target_s, hi))
        if self.resync:
            self._splice(target_s)
        else:
            self.rebased = False
            self.wall_anchor_clock_s = target_s
            self.wall_anchor_time = time.time()
            if sc.source is not None:
                sc.source.request_seek(target_s)
        sc.osd.post(f"SEEK {timecode(target_s)}")

    def loop_toggle(self) -> None:
        """3-state cycle: mark A -> mark B + start looping -> clear.

        Drives the same loop_a/loop_b/loop_state machine as the Record/Stop
        pair, so the single-button and Record/Stop workflows give identical
        feedback."""
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
        """Red border while a loop is armed.

        A poke, not part of the frame: a display mode that pushes $D020
        (hires, mcm, petscii, blank) replaces it while the loop is still
        armed, whenever that push sends — on a change of its own value, or
        after a lost write. Clearing it drops that push's cache entry, so a
        mode whose border is not black puts its own back on its next push."""
        if active == self.record_border_active:
            return
        self.record_border_active = active
        api = self._scene.api
        api.write_regs("d020", RECORD_BORDER_COLOR if active else 0)
        if not active:
            api.invalidate_region(RegionID.VIC_D020)

    def record(self) -> None:
        """Record button: arm a loop at the current position (first step of
        the Record -> Stop workflow; see stop()). A no-op beyond the usual
        transport touch if a loop is already armed or active."""
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
        exclusive, both False on a plain press (recall).

        Save and clear post no OSD; recall does. See
        docs/architecture/scenes.md#record-workflow--loop-preset-pads-midi-live-tune-phase-3.
        """
        sc = self._scene
        if clear:
            if self.loop_store is not None:
                self.loop_store.delete(slot)
            log.info("transport: loop slot %d cleared", slot)
            return
        if save:
            if self.loop_a is None:
                log.info("transport: loop slot %d save ignored — no loop marked", slot)
                return
            if self.loop_store is not None:
                self.loop_store.save(slot, self.loop_a, self.loop_b)
            log.info("transport: loop %s saved to slot %d", timecode(self.loop_a), slot)
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
        """The playback position in content seconds, which is what the whole
        transport surface speaks; the internal clock is in the scaled/PTS
        domain, offset by start_s until the first seek (see clock_to_content).

        On the resync path a splice holds the clock below its target until the
        target is heard; this reports the target through that hold, because a
        held FF/RW and a relative jog seek to ``position() + delta`` and would
        otherwise lose the hold's length on every step."""
        return self.clock_to_content(self.target_clock_s())

    def target_clock_s(self, clock_s: float | None = None) -> float:
        """clock_s(), except through a resync splice's hold, where it is the
        splice target the clock is waiting to reach. The displayed frame is
        chosen by it, so a seek shows its target frame as a still through the
        hold rather than nothing: a held FF/RW re-seeks faster than a hold
        ends and would otherwise show no picture until release.

        A caller that already read ``clock_s`` passes it, and outside a hold
        gets that same value back; a second read of a running clock differs."""
        if not (self.touched and self.resync):
            return self.clock_s() if clock_s is None else clock_s
        anchor = self.audio_anchor
        clk = self._resync_clock_s(anchor) if clock_s is None else clock_s
        return max(clk, anchor[0])

    def duration(self) -> float | None:
        source = self._scene.source
        return source.duration_s if source is not None else None

    def is_paused(self) -> bool:
        return self.paused

    def loop_info(self) -> dict[str, float | str | None]:
        """The A/B loop machine's state, for a console's scrub bar to draw the
        marks and its own loop button to reflect. Content-seconds, matching
        :meth:`position` — not the internal (possibly scaled) clock domain."""
        return {"state": self.loop_state, "a": self.loop_a, "b": self.loop_b}

    def loop_slots(self) -> list[int]:
        """Which pad numbers hold a saved loop preset, so a console lights a
        recall pad only where there is something to recall — mirrors
        ``PerformanceEngine.saved_look_slots``."""
        if self.loop_store is None:
            return []
        return sorted(int(k) for k in self.loop_store.load())
