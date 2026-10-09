- **A page URL mixed into a multi-entry `file =` spec was never resolved.**
  Only a whole-spec URL goes through yt-dlp, so such an entry stayed in the
  candidate pool as a raw page URL for PyAV to open as a media file — the exact
  cryptic `Invalid data found` failure the offline pre-check exists to prevent,
  and it happened even with the `yt` extra installed. It is refused at validate
  time with a message saying to give the URL a scene of its own.
