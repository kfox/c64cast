"""Offline tests for c64cast.app.recording_metadata — no U64 hardware, no I/O.

Run:    python -m unittest discover tests
   or:  python -m unittest tests.test_recording_metadata
"""

# _FakeVideoScene / _FakeWaveformScene are intentional duck-typed Scene
# stubs (same convention as test_playlist.py's FakeScene) — silence
# pyright's structural-typing complaints rather than spraying ignores.
# pyright: reportArgumentType=false

from __future__ import annotations

import json
import unittest

from c64cast.app.config import Config, SceneCfg
from c64cast.app.quickcast import ResolvedMedia
from c64cast.app.recording_metadata import (
    _UNKNOWN_COPYRIGHT,
    SCENE_CONFIG_MARKER,
    build_scene_recording_metadata,
    extract_scene_configs,
    log_scene_recording_metadata,
    render_description,
)
from c64cast.sid.sid_host_emu import SidHeader


class _FakeVideoScene:
    def __init__(
        self,
        name,
        file_spec,
        filepath,
        display_mode=None,
        source_info: ResolvedMedia | None = None,
        source=None,
    ):
        self._cfg = SceneCfg(type="video", file=file_spec)
        self.name = name
        self.filepath = filepath
        self.display_mode = display_mode
        self.target_fps = None
        self.duration_s = float("inf")
        self.audio = object()
        self.effect = None
        self.overlays = []
        self.source_info = source_info
        self.source = source  # a fake AVFileSource, for local container-tag tests


class _FakeWaveformScene:
    def __init__(self, name, sid_file, header):
        self._cfg = SceneCfg(type="waveform", file=sid_file)
        self.name = name
        self._sid_file = sid_file
        self.header = header
        self.display_mode = None
        self.target_fps = 30.0
        self.duration_s = 30.0
        self.audio = object()
        self.effect = None
        self.overlays = []


def _make_header(name="Wizball", author="Martin Galway", released="1987 Ocean") -> SidHeader:
    return SidHeader(
        magic="PSID",
        version=2,
        num_songs=1,
        start_song=1,
        name=name,
        author=author,
        released=released,
        clock="PAL",
        sid_model="6581",
    )


class _FakeContainer:
    def __init__(self, metadata):
        self.metadata = metadata


class _FakeAVFileSource:
    """Stands in for VideoScene.source — only .container.metadata is read."""

    def __init__(self, metadata):
        self.container = _FakeContainer(metadata)


class VideoSourceTest(unittest.TestCase):
    def test_url_scene_carries_original_link_and_placeholder_copyright(self):
        scene = _FakeVideoScene(
            "My Clip", "https://youtu.be/abc123", "https://cdn.example.com/stream.mp4"
        )
        cfg = Config()
        payload = build_scene_recording_metadata(scene, cfg, "system")
        source = payload["source"]
        self.assertEqual(source["url"], "https://youtu.be/abc123")
        self.assertIsNone(source["local_file"])
        self.assertEqual(source["copyright"], _UNKNOWN_COPYRIGHT)
        # The resolved CDN stream URL must never leak into the blob.
        self.assertNotIn("cdn.example.com", json.dumps(payload))

    def test_url_scene_reports_ytdlp_license_verbatim(self):
        scene = _FakeVideoScene(
            "My Clip",
            "https://youtu.be/abc123",
            "https://cdn.example.com/stream.mp4",
            source_info=ResolvedMedia(
                stream_url="https://cdn.example.com/stream.mp4",
                kind="video",
                uploader="Some Channel",
                license="Creative Commons Attribution license (reuse allowed)",
                webpage_url="https://youtu.be/abc123",
            ),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        source = payload["source"]
        self.assertEqual(
            source["copyright"], "Creative Commons Attribution license (reuse allowed)"
        )
        self.assertEqual(source["uploader"], "Some Channel")
        self.assertEqual(source["webpage_url"], "https://youtu.be/abc123")

    def test_url_scene_with_uploader_but_no_license_names_uploader_as_lead(self):
        scene = _FakeVideoScene(
            "My Clip",
            "https://youtu.be/abc123",
            "https://cdn.example.com/stream.mp4",
            source_info=ResolvedMedia(
                stream_url="https://cdn.example.com/stream.mp4",
                kind="video",
                uploader="Some Channel",
            ),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        self.assertIn("Some Channel", payload["source"]["copyright"])
        self.assertIn("unknown", payload["source"]["copyright"])

    def test_local_video_scene_has_no_url(self):
        scene = _FakeVideoScene("clip.mp4", "assets/videos/clip.mp4", "assets/videos/clip.mp4")
        cfg = Config()
        payload = build_scene_recording_metadata(scene, cfg, "system")
        source = payload["source"]
        self.assertIsNone(source["url"])
        self.assertEqual(source["local_file"], "clip.mp4")
        self.assertEqual(source["copyright"], _UNKNOWN_COPYRIGHT)

    def test_local_video_scene_reports_container_copyright_tag(self):
        scene = _FakeVideoScene(
            "clip.mp4",
            "assets/videos/clip.mp4",
            "assets/videos/clip.mp4",
            source=_FakeAVFileSource({"copyright": "© 2026 Some Studio"}),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        self.assertEqual(payload["source"]["copyright"], "© 2026 Some Studio")

    def test_local_video_scene_falls_back_to_rights_then_license_tag(self):
        scene = _FakeVideoScene(
            "clip.mp4",
            "assets/videos/clip.mp4",
            "assets/videos/clip.mp4",
            source=_FakeAVFileSource({"license": "Public Domain"}),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        self.assertEqual(payload["source"]["copyright"], "Public Domain")

    def test_local_video_scene_with_unrelated_container_tags_stays_unknown(self):
        scene = _FakeVideoScene(
            "clip.mp4",
            "assets/videos/clip.mp4",
            "assets/videos/clip.mp4",
            source=_FakeAVFileSource({"encoder": "Lavf62.12.101"}),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        self.assertEqual(payload["source"]["copyright"], _UNKNOWN_COPYRIGHT)


class WaveformSourceTest(unittest.TestCase):
    def test_sid_header_fields_used_verbatim_no_placeholder(self):
        header = _make_header()
        scene = _FakeWaveformScene("SID: Wizball #1", "assets/sids/Wizball.sid", header)
        cfg = Config()
        payload = build_scene_recording_metadata(scene, cfg, "system")
        source = payload["source"]
        self.assertEqual(source["sid_name"], "Wizball")
        self.assertEqual(source["sid_author"], "Martin Galway")
        self.assertEqual(source["sid_released"], "1987 Ocean")
        self.assertEqual(source["local_file"], "Wizball.sid")
        self.assertNotIn("copyright", source)


class RedactionTest(unittest.TestCase):
    def test_hardware_block_excludes_connection_info(self):
        cfg = Config()
        cfg.ultimate64.url = "http://192.168.2.64"
        cfg.ultimate64.dma_password = "s3cr3t"
        cfg.teensyrom.host = "192.168.2.99"
        scene = _FakeVideoScene("clip.mp4", "assets/videos/clip.mp4", "assets/videos/clip.mp4")
        payload = build_scene_recording_metadata(scene, cfg, "system")
        blob = json.dumps(payload)
        self.assertNotIn("192.168.2.64", blob)
        self.assertNotIn("s3cr3t", blob)
        self.assertNotIn("192.168.2.99", blob)
        self.assertEqual(set(payload["hardware"].keys()), {"backend", "system", "sid_model"})


class LogAndParseRoundTripTest(unittest.TestCase):
    def test_log_line_round_trips_through_extract_scene_configs(self):
        scene = _FakeVideoScene("clip.mp4", "assets/videos/clip.mp4", "assets/videos/clip.mp4")
        cfg = Config()
        with self.assertLogs("c64cast.recording", level="INFO") as cm:
            log_scene_recording_metadata(scene, cfg, "system")
        self.assertEqual(len(cm.output), 1)
        self.assertIn(SCENE_CONFIG_MARKER, cm.output[0])

        # The file handler prefixes asctime/name/levelname — simulate that
        # to prove extract_scene_configs doesn't depend on message being
        # the whole line.
        prefixed = f"12:00:00 c64cast.recording INFO: {cm.output[0].split(':', 1)[1].strip()}"
        entries = extract_scene_configs(prefixed)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scene"]["name"], "clip.mp4")

    def test_no_cfg_logs_nothing(self):
        scene = _FakeVideoScene("clip.mp4", "assets/videos/clip.mp4", "assets/videos/clip.mp4")
        with self.assertNoLogs("c64cast.recording", level="INFO"):
            log_scene_recording_metadata(scene, None, "system")

    def test_extract_ignores_unrelated_and_malformed_lines(self):
        text = "some unrelated log line\nc64cast.recording INFO: SCENE_CONFIG_JSON {not json\n"
        self.assertEqual(extract_scene_configs(text), [])


class RenderDescriptionTest(unittest.TestCase):
    def test_render_includes_key_fields(self):
        scene = _FakeVideoScene(
            "My Clip", "https://youtu.be/abc123", "https://cdn.example.com/stream.mp4"
        )
        cfg = Config()
        payload = build_scene_recording_metadata(scene, cfg, "system")
        text = render_description(payload)
        self.assertIn("My Clip", text)
        self.assertIn("https://youtu.be/abc123", text)
        self.assertIn(_UNKNOWN_COPYRIGHT, text)
        # The blob is written to be pasted into a public description, so the
        # copyright line has to survive being published unedited.
        self.assertNotIn("TODO", text)
        self.assertIn("c64cast", text)

    def test_render_includes_uploader_and_real_license(self):
        scene = _FakeVideoScene(
            "My Clip",
            "https://youtu.be/abc123",
            "https://cdn.example.com/stream.mp4",
            source_info=ResolvedMedia(
                stream_url="https://cdn.example.com/stream.mp4",
                kind="video",
                uploader="Some Channel",
                license="Public Domain",
                webpage_url="https://youtu.be/abc123",
            ),
        )
        payload = build_scene_recording_metadata(scene, Config(), "system")
        text = render_description(payload)
        self.assertIn("Some Channel", text)
        self.assertIn("Public Domain", text)
        # webpage_url equals the original link here, so it must not repeat.
        self.assertEqual(text.count("https://youtu.be/abc123"), 1)


if __name__ == "__main__":
    unittest.main()
