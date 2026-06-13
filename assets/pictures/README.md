# assets/pictures/

Still images for the [SlideshowScene](../../c64cast/scenes.py). Any format
OpenCV's `cv2.imread` can decode works — `.jpg`, `.jpeg`, `.png`, `.bmp`,
`.webp`. Images are center-cropped to the C64's 320:200 aspect ratio and
quantized to the VIC-II palette by the scene's display mode.

The scene cycles through the resolved pool with a shuffle-and-walk picker
(every image is shown once before any repeats; no immediate back-to-back
repeats across reshuffle boundaries). Per-image display time is controlled
by `image_duration_s` (default 5 s); total scene runtime by `duration_s`.

Reference one or many from a config:

```toml
[[scenes]]
type = "slideshow"
display = "mhires"           # or "hires", "petscii", "mcm", "random"
file = "assets/pictures/photo.jpg"          # single image
# file = "assets/pictures"                  # whole directory (default)
# file = "assets/pictures/*.png"            # glob
# file = "assets/pictures, assets/extra/*.jpg"  # combination
duration_s = 60.0
image_duration_s = 5.0
```

Omit `file =` entirely to fall back to scanning this directory.
