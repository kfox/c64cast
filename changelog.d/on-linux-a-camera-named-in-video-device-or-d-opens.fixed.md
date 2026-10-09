- **On Linux, a camera named in `[video].device` or `-d` opens through V4L2.**
  A camera is listed once per Linux capture backend, so a name or VID:PID
  matched it twice, warned that it matched 2 cameras, and opened its GStreamer
  index (`1800 + N` in `--list-devices`), which OpenCV's pip wheels cannot
  open. It now counts as one camera and opens at its V4L2 index (`200 + N`).
  A name that matches several cameras still warns and takes the first.
