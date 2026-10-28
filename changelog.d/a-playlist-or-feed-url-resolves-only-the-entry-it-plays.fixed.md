- **A playlist or feed URL resolves only the entry it plays.** A video `file`
  URL (or a quick-playback argument) naming a playlist or an RSS feed played its
  first entry, but yt-dlp fetched every entry first, each with its own 20 s
  timeout and retries, while the C64 sat in reset; a long playlist or a slow
  site held the machine there for minutes. Only the first entry is fetched now.
