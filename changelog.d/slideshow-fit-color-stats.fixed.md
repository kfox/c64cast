- **A slideshow in `aspect_mode = "fit"` no longer counts its black bars as
  part of the picture.** `auto_fit`, `force_palette` and
  `hardware_palette = "source"` read the letterboxed frame, so the bars pulled
  the fitted black point and saturation down and added black to the palette.
  They now read only the picture.
