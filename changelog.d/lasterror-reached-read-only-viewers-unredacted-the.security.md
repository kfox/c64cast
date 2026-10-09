- `last_error` reached read-only viewers unredacted. The supervisor stored
  raw exception text and `SessionStatus.as_dict()` shipped it verbatim into
  the `session` key of every `/api/ws` frame and of `GET /api/session`, both
  of which a `viewer` credential may read — so a build failure whose message
  quoted a connection URL with its `?query` link knobs, or a value a library
  echoed back, was handed to a guest whose credential exists specifically to
  withhold control. The sibling `log` key on that same frame was already
  passed through `redact_secrets` on the way in, with a comment saying
  exactly why; this was the one field on the frame that bypassed it.
