- **A video that switches resolution mid-stream no longer fills memory with
  full-size frames.** The decode downscale was planned from the first frame
  alone, so a stream that opened small and then switched to 4K or 8K had
  every later frame converted at full size, and the video buffer held up to
  240 of them: gigabytes from one crafted file or URL. The downscale is now
  planned again each time the frame size changes, for playback and for the
  color pre-scan.
