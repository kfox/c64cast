"""Tests for SidFileAudioSource — the composable AudioSource that plays a .sid
on the real chip, plus the sid_host_emu structural helpers it shares with
WaveformScene and the import-weight guard that keeps audio_source light."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import cast

from _fakes import FakeAPI

from c64cast.audio_source import SidFileAudioSource
from c64cast.backend import C64Backend
from c64cast.sid_host_emu import (
    _play_bank_for_footprints,
    _sid_payload_extent,
    payload_overlaps_bank0_display,
    sid_play_preflight,
)


def _make_sid(
    *, init=0x1000, play=0x1001, payload=(0x60, 0x60), load=0x1000, num_songs=1, start_song=1
) -> bytes:
    """Minimal PSID v2 with real init/play addresses + a payload — runnable by
    SidHostEmu (parse_psid_for_player + INIT/PLAY)."""
    h = bytearray(124)
    h[0:4] = b"PSID"
    h[4:6] = (2).to_bytes(2, "big")  # version
    h[6:8] = (0x7C).to_bytes(2, "big")  # data offset
    h[8:10] = load.to_bytes(2, "big")
    h[10:12] = init.to_bytes(2, "big")
    h[12:14] = play.to_bytes(2, "big")
    h[14:16] = num_songs.to_bytes(2, "big")
    h[16:18] = start_song.to_bytes(2, "big")
    return bytes(h) + bytes(payload)


class _FakeMode:
    """Stand-in display mode — only `is_bitmapped` matters to the SID source."""

    def __init__(self, is_bitmapped: bool):
        self.is_bitmapped = is_bitmapped


class SidHostEmuHelpersTest(unittest.TestCase):
    def test_payload_extent_basic(self):
        sid = _make_sid(load=0x1000, payload=(0x60,) * 0x100)
        lo, hi = _sid_payload_extent(sid)
        self.assertEqual(lo, 0x1000)
        self.assertEqual(hi, 0x1000 + 0x100)

    def test_overlap_char_only_screen(self):
        # A $1000-load tune of 0x1500 bytes reaches $2500 — clears $0400 (char)
        # but overlaps the $2000 bitmap.
        sid = _make_sid(load=0x1000, payload=(0x60,) * 0x1500)
        self.assertIsNone(payload_overlaps_bank0_display(sid, is_bitmapped=False))
        conflict = payload_overlaps_bank0_display(sid, is_bitmapped=True)
        assert conflict is not None
        self.assertEqual(conflict, (0x2000, 0x2000 + 8000))

    def test_overlap_screen_region(self):
        # Load right at screen RAM ($0400) → conflicts on a char display too.
        sid = _make_sid(load=0x0400, init=0x0400, play=0x0401, payload=(0x60, 0x60))
        conflict = payload_overlaps_bank0_display(sid, is_bitmapped=False)
        assert conflict is not None
        self.assertEqual(conflict[0], 0x0400)

    def test_high_load_clears_both(self):
        sid = _make_sid(load=0x4000, init=0x4000, play=0x4001, payload=(0x60, 0x60))
        self.assertIsNone(payload_overlaps_bank0_display(sid, is_bitmapped=True))

    def test_play_bank_intersection(self):
        # write_fp ∩ access_fp inside BASIC ROM → $36; disjoint → None.
        w = bytearray(0x10000)
        a = bytearray(0x10000)
        self.assertIsNone(_play_bank_for_footprints(w, a))
        w[0xB400] = 1
        a[0xB400] = 1
        self.assertEqual(_play_bank_for_footprints(w, a), 0x36)
        # A write outside the BASIC window doesn't trigger it.
        w2 = bytearray(0x10000)
        a2 = bytearray(0x10000)
        w2[0x1000] = 1
        a2[0x1000] = 1
        self.assertIsNone(_play_bank_for_footprints(w2, a2))

    def test_preflight_accepts_returning_play(self):
        sid = _make_sid(init=0x1000, play=0x1001, payload=(0x60, 0x60))
        self.assertTrue(sid_play_preflight(sid))

    def test_preflight_rejects_spinning_play(self):
        # play=$1001 JMP $1001 → caps every pass.
        sid = _make_sid(init=0x1000, play=0x1001, payload=(0x60, 0x4C, 0x01, 0x10))
        self.assertFalse(sid_play_preflight(sid))


class SidFileAudioSourceTest(unittest.TestCase):
    def _write(self, sid_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".sid")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(sid_bytes)
        self.addCleanup(os.remove, path)
        return path

    def _src(self, path, *, is_bitmapped=False, song=0):
        return SidFileAudioSource(
            cast(C64Backend, FakeAPI()),
            path,
            song=song,
            display_mode=cast("object", _FakeMode(is_bitmapped)),  # type: ignore[arg-type]
        )

    def test_wants_audio_lock_and_no_clock(self):
        path = self._write(_make_sid())
        src = self._src(path)
        self.assertTrue(src.wants_audio_lock)
        self.assertIsNone(src.position_seconds())

    def test_char_display_accepts_low_load(self):
        # Typical $1000-load tune; char display reserves only $0400 → OK.
        path = self._write(_make_sid(load=0x1000, payload=(0x60,) * 0x1500))
        src = self._src(path, is_bitmapped=False)  # must not raise
        self.assertEqual(src.song, 1)

    def test_song_out_of_range_rejected(self):
        import logging

        path = self._write(_make_sid(num_songs=2))
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with self.assertRaisesRegex(ValueError, "out of range"):
            self._src(path, song=5)

    def test_bitmap_display_rejects_overlapping_payload(self):
        path = self._write(_make_sid(load=0x1000, payload=(0x60,) * 0x1500))
        # The single-candidate skip logs a WARNING before the hard raise;
        # assertLogs captures it so the console stays clean.
        with self.assertLogs("c64cast.audio_source", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "hires bitmap"):
                self._src(path, is_bitmapped=True)

    def test_setup_runs_player_with_avoid_and_play_bank(self):
        path = self._write(_make_sid(load=0x1000, payload=(0x60,) * 0x40))
        api = FakeAPI()
        src = SidFileAudioSource(
            cast(C64Backend, api),
            path,
            song=0,
            display_mode=cast("object", _FakeMode(False)),  # type: ignore[arg-type]
        )
        src.setup()
        assert api.sid_played is not None
        # avoid is a 64 KB bitmap reserving screen RAM ($0400).
        avoid = api.sid_played_avoid
        assert avoid is not None
        self.assertEqual(len(avoid), 0x10000)
        self.assertTrue(all(avoid[0x0400 : 0x0400 + 1000]))
        # Char display doesn't reserve the bitmap — leave $2000 free for the
        # player MC if it wants it.
        self.assertFalse(any(avoid[0x2000 : 0x2000 + 8000]))

    def test_setup_bitmap_reserves_bitmap_region(self):
        # High-load SID so the bitmap display accepts it; then avoid must
        # reserve both $0400 and $2000.
        path = self._write(_make_sid(load=0x4000, init=0x4000, play=0x4001, payload=(0x60, 0x60)))
        api = FakeAPI()
        src = SidFileAudioSource(
            cast(C64Backend, api),
            path,
            display_mode=cast("object", _FakeMode(True)),  # type: ignore[arg-type]
        )
        src.setup()
        avoid = api.sid_played_avoid
        assert avoid is not None
        self.assertTrue(all(avoid[0x2000 : 0x2000 + 8000]))

    def test_teardown_silences_in_order(self):
        path = self._write(_make_sid())
        api = FakeAPI()
        # Record call order: vector restore MUST precede silence (so a PLAY
        # tick can't rewrite the SID between the volume-clear and gate-clears).
        order: list[str] = []
        for name in ("restore_kernal_irq_vector", "silence_sid", "suppress_cursor_blink"):
            orig = getattr(api, name)

            def wrap(orig=orig, name=name):
                def _f(*a, **k):
                    order.append(name)
                    return orig(*a, **k)

                return _f

            setattr(api, name, wrap())
        src = SidFileAudioSource(
            cast(C64Backend, api),
            path,
            display_mode=cast("object", _FakeMode(False)),  # type: ignore[arg-type]
        )
        src.teardown()
        self.assertEqual(
            order, ["restore_kernal_irq_vector", "silence_sid", "suppress_cursor_blink"]
        )

    def test_pool_retry_skips_bad_candidate(self):
        # A directory with one spinning SID + one healthy SID: the source must
        # skip the bad one and pick the good one. Stub the shuffle to an
        # in-place sort so "bad.sid" (sorts first) is tried first deterministically.
        from unittest.mock import patch

        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        with open(os.path.join(d, "bad.sid"), "wb") as f:
            f.write(_make_sid(init=0x1000, play=0x1001, payload=(0x60, 0x4C, 0x01, 0x10)))
        with open(os.path.join(d, "good.sid"), "wb") as f:
            f.write(_make_sid(init=0x1000, play=0x1001, payload=(0x60, 0x60)))
        # The skip logs a warning — assertLogs both verifies the skip AND keeps
        # the console clean.
        with patch("c64cast.audio_source.random.shuffle", lambda x: x.sort()):
            with self.assertLogs("c64cast.audio_source", level="WARNING"):
                src = self._src(d + "/*.sid", is_bitmapped=False)
        self.assertEqual(os.path.basename(src._sid_file), "good.sid")

    def test_all_candidates_bad_raises(self):
        import logging

        path = self._write(_make_sid(init=0x1000, play=0x1001, payload=(0x60, 0x4C, 0x01, 0x10)))
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with self.assertRaisesRegex(ValueError, "none could be loaded"):
            self._src(path, is_bitmapped=False)


class ConfigSidGenerativeTest(unittest.TestCase):
    """build_scene wiring for `type = generative, audio_source = sid`."""

    def _write(self, sid_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".sid")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(sid_bytes)
        self.addCleanup(os.remove, path)
        return path

    def setUp(self):
        from c64cast.config import Config

        self.cfg = Config()
        # A small $1000-load tune clears $0400; works on a char display.
        self.sid = self._write(_make_sid(load=0x1000, payload=(0x60,) * 0x40))

    def _build(self, cfg=None, **kw):
        from c64cast.config import SceneCfg, build_scene

        s = SceneCfg(type="generative", source="plasma", audio_source="sid", file=self.sid, **kw)
        return build_scene(s, cfg or self.cfg, cast(C64Backend, FakeAPI()), None, None)

    def test_sid_scene_is_host_dma_and_competes(self):
        from c64cast.modes import PETSCIIDisplayMode
        from c64cast.scenes import SourceScene

        scene = self._build(display="petscii")
        assert isinstance(scene, SourceScene)
        self.assertIsNone(scene.audio)  # no DAC streamer for a SID source
        self.assertTrue(scene.competes_for_audio_lock())
        assert isinstance(scene.display_mode, PETSCIIDisplayMode)
        self.assertFalse(scene.display_mode.use_reu_staged)
        self.assertIs(scene.audio_source.wants_audio_lock, True)

    def test_force_host_dma_overrides_explicit_reu_true(self):
        from c64cast.config import Config
        from c64cast.modes import PETSCIIDisplayMode

        cfg = Config()
        cfg.video.use_reu_staged = True
        scene = self._build(cfg=cfg, display="petscii")
        assert isinstance(scene.display_mode, PETSCIIDisplayMode)
        self.assertFalse(scene.display_mode.use_reu_staged)

    def test_explicit_audio_with_sid_rejected(self):
        from c64cast.config import SceneCfg, build_scene

        s = SceneCfg(
            type="generative", audio_source="sid", file=self.sid, display="petscii", audio=False
        )
        with self.assertRaisesRegex(ValueError, "audio_source = 'sid'"):
            build_scene(s, self.cfg, cast(C64Backend, FakeAPI()), None, None)

    def test_bitmap_sid_defaults_to_half_rate(self):
        # A high-load tune so the bitmap display accepts it; target_fps halves.
        hi = self._write(_make_sid(load=0x4000, init=0x4000, play=0x4001, payload=(0x60, 0x60)))
        from c64cast.config import SceneCfg, build_scene

        s = SceneCfg(type="generative", audio_source="sid", file=hi, display="mhires")
        scene = build_scene(s, self.cfg, cast(C64Backend, FakeAPI()), None, None)
        self.assertEqual(scene.target_fps, 30.0)  # NTSC half-rate

    def test_explicit_target_fps_wins_over_half_rate(self):
        hi = self._write(_make_sid(load=0x4000, init=0x4000, play=0x4001, payload=(0x60, 0x60)))
        from c64cast.config import SceneCfg, build_scene

        s = SceneCfg(
            type="generative", audio_source="sid", file=hi, display="mhires", target_fps=12.0
        )
        scene = build_scene(s, self.cfg, cast(C64Backend, FakeAPI()), None, None)
        self.assertEqual(scene.target_fps, 12.0)

    def test_ensemble_does_not_suppress_sid_source(self):
        # A SID source legitimately holds the audio spotlight; ensemble mode
        # must not null it out (wants_audio_lock gates the slot instead).
        scene = self._build(display="petscii")  # is_ensemble defaults False
        from c64cast.config import SceneCfg, build_scene

        s = SceneCfg(type="generative", audio_source="sid", file=self.sid, display="petscii")
        scene = build_scene(s, self.cfg, cast(C64Backend, FakeAPI()), None, None, is_ensemble=True)
        self.assertTrue(scene.competes_for_audio_lock())

    def test_validate_load_time_rejects_bitmap_overlap(self):
        # A typical $1000-load tune with a real-sized payload overlaps $2000 —
        # rejected at validate_scene_cfg time on a bitmap display.
        from c64cast.config import SceneCfg, validate_scene_cfg

        big = self._write(_make_sid(load=0x1000, payload=(0x60,) * 0x1500))
        s = SceneCfg(type="generative", audio_source="sid", file=big, display="mhires")
        with self.assertRaisesRegex(ValueError, "hires bitmap"):
            validate_scene_cfg(s, self.cfg, audio_enabled=False)


class AudioSourceImportWeightTest(unittest.TestCase):
    """audio_source must stay light: importing it (done for every SourceScene,
    including mic/null) must NOT drag in the oscilloscope renderer / numpy /
    py65. The SID helpers are lazy-imported inside SidFileAudioSource methods."""

    def test_import_does_not_pull_heavy_modules(self):
        import subprocess
        import sys

        code = (
            "import sys; import c64cast.audio_source; "
            "heavy=[m for m in ('c64cast.waveform','numpy','py65','c64cast.sid_host_emu',"
            "'c64cast.voice_scope') if m in sys.modules]; "
            "print(','.join(heavy))"
        )
        out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        self.assertEqual(out, "", f"audio_source import pulled in heavy modules: {out}")


if __name__ == "__main__":
    unittest.main()
