# assets/models/

ML model files for the **vision controller** (webcam hand-gesture control —
see [c64cast/vision.py](../../c64cast/vision.py)). Like the rest of
`assets/`, the model files themselves are `.gitignore`d; only this README is
tracked.

## hand_landmarker.task

The vision controller uses Google MediaPipe's **HandLandmarker** task bundle.
Download it once and drop it here (the default `[vision].model_path`):

```bash
curl -L -o assets/models/hand_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
```

Then enable gesture control:

```toml
[vision]
enabled = true
# model_path defaults to assets/models/hand_landmarker.task
```

You also need the `vision` extra installed (MediaPipe):

```bash
uv sync --extra vision
```

If the model file is missing (or mediapipe isn't installed), the vision
controller logs a clear error and the stream runs without gesture control —
it never crashes the playlist.
