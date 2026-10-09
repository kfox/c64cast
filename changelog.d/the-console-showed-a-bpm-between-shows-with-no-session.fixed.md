- **The console showed a BPM between shows.** With no session running, the page
  stopped the beat pulse but left the tempo number alone, so the sticky header
  read the last show's BPM — or a confident `120` from the page's own
  initializer, before any frame had arrived — above "No session running." It
  reads `--`.
