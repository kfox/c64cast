- **One unreadable duration in `Songlengths.md5` no longer throws the whole
  database away.** A duration with thousands of digits, or one too large to
  hold as a number, failed the load, and every SID scene fell back to its
  default length; it now leaves only that subtune's length unknown.
