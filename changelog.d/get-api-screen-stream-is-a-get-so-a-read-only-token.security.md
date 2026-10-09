- `GET /api/screen/stream` is a `GET`, so a read-only token reached it, and
  nothing capped concurrent watchers. Each open stream holds one thread of
  the *default* executor essentially continuously (the fps sleep happens
  inside the frame generator), and that executor is also where media-upload
  chunk writes and every synchronous route run — so a dozen parallel
  requests from one viewer credential starved the whole console, with a
  healthy process and an empty log. The streams now have a dedicated
  bounded pool and a watcher cap, refusing past it with `503`.
