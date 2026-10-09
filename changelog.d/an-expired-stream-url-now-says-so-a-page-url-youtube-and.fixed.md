- **An expired stream URL now says so.** A page URL (YouTube and friends) is
  resolved to a signed stream URL once, when the playlist is built, and the
  playlist replays that same URL on every loop — so a show running longer than
  the signature's lifetime starts failing with a bare `HTTPForbiddenError` that
  explains nothing. A remote 4xx now names the likely cause and the remedy:
  reload the playlist (SIGHUP, or `POST /reload`) to re-resolve it — advice
  limited to the 401/403 a stale signature actually answers with, since a 404
  is a pulled video and points somewhere else entirely. The URL itself is not
  quoted back, since it can carry a signature or credential: PyAV appends the
  filename to the exception's own string form, so the message is built from
  `strerror` rather than from the exception.
