# assets/videos/

Source video files for [CommercialScene](../../c64cast/scenes.py) playback
via PyAV. Directory/glob scans recognize `.mp4`, `.avi`, `.mkv`, `.mov`,
`.webm`, and `.m4v` (an explicit `file =` path can point at any container PyAV
can demux). Audio is resampled to the SID DAC rate (typically 8 kHz) and the
video frame closest to the audio playback position is picked each tick, so A/V
stays in sync without drift.

Reference one from a config:

```toml
[[scenes]]
type = "commercial"
display = "mhires"
file = "assets/videos/my-video.webm"
```

Or drop multiple files in here and let [playlist].ads_dir = "assets/videos"
interleave them between webcam scenes. Requires the `commercials` extra
(`pip install c64cast[commercials]`).
