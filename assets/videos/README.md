# assets/videos/

Source video files for [VideoScene](../../c64cast/scenes.py) playback
via PyAV. Directory/glob scans recognize `.mp4`, `.avi`, `.mkv`, `.mov`,
`.webm`, and `.m4v` (an explicit `file =` path can point at any container PyAV
can demux). Audio is resampled to the SID DAC rate (typically 8 kHz) and the
video frame closest to the audio playback position is picked each tick, so A/V
stays in sync without drift.

Reference one from a config:

```toml
[[scenes]]
type = "video"
display = "mhires"
file = "assets/videos/my-video.webm"
```

Or drop multiple files in here and let [playlist].videos_dir = "assets/videos"
interleave them between webcam scenes. Requires the `video` extra
(`pip install c64cast[video]`).
