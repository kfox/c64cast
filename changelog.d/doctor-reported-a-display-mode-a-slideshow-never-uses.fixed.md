- **`--doctor` reported a display mode a slideshow never uses, and skipped it
  from three color reports.** `resolve_scene_display` answered `hires_edges` for
  a default-display slideshow while the build resolved `mhires`, and doctor
  branches on that answer — dropping the `color_match` report for `hires_edges`
  and the `cell_strategy`/`motion_smoothing` reports for anything but `mhires`.
  The one scene type whose `auto` resolutions actually differ was therefore the
  one type missing from all three. It delegates to the slideshow resolver now.
