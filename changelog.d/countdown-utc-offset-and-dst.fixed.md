- **A `countdown` target with a UTC offset now counts down, and one across a
  daylight-saving change is no longer an hour out.** A target such as
  `2026-12-31T23:00:00Z` disabled the overlay on its first frame, and a local
  target past a clock change showed wall-clock rather than elapsed time.
