- **A live scene beside a `follower_only` sibling tore down every 30 seconds.**
  The single-scene duration default counted `[[scenes]]` entries, but
  follower-only scenes never reach the playlist and the playlist's own
  single-scene mode counts what it was handed. So the canonical ensemble shape —
  one webcam or blank scene plus a follower-only sibling — really was a
  single-scene playlist, yet the scene kept the finite 30 s default and
  re-opened the capture device every 30 seconds forever (and under `--no-loop`
  the show simply ended). It counts the rotation now.
