- **Webcam clips could never launch from the `[[performance.clips]]` grid.**
  `type` defaults to `"webcam"` in a clip table, but the decision to open the
  camera only looked at `[[scenes]]` and `[vision]`. With no webcam scene
  declared and vision off, the camera stayed shut, and the pad's scene build
  failed on a background thread — logged, then swallowed into the pad's error
  state, so the pad simply never fired for the whole show. Clips now count
  toward opening the camera.
