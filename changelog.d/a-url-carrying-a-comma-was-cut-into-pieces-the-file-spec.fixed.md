- **A URL carrying a comma was cut into pieces.** The `file =` spec was split
  on every comma before anything looked at the scheme, so the standard Akamai
  HLS shape (`.../clip_,500,800,.mp4.csmil/master.m3u8`) became a truncated URL
  plus fragments reported as paths with the wrong extension — naming things the
  user never typed. yt-dlp's own resolved stream URLs, which a URL video scene
  writes back into its file spec, routinely carry commas in query parameters.
  After a URL entry a comma now separates only when the next fragment announces
  a new entry (it begins with whitespace, or is itself a URL), so
  `http://h/a.mp4, b.mp4` is still two entries and `http://h/a.mp4,b.mp4` is
  one URL.
