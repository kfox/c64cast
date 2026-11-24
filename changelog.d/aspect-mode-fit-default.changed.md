- **Videos and slideshows now show the whole picture by default.** The
  default `aspect_mode` is `fit`, not `crop`: a landscape source keeps its
  full width and a portrait one its full height, with black bars filling the
  rest of the screen. A widescreen film on an NTSC set used to lose a third of
  its width to the crop. Set `aspect_mode = "crop"` on a scene to fill the
  screen as before.
