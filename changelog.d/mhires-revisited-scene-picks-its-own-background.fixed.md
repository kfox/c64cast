- **A looping mhires or mcm scene no longer opens on the last lap's background
  colors.** Re-entering the scene carried the previous lap's color counts into
  its first frames, so `$D021` (and, in mcm, `$D022`-`$D023`) held the old
  dominant colors until they decayed.
