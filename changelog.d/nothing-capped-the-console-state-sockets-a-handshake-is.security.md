- **Nothing capped the console state sockets.** A handshake is a bare `GET`, so
  the role gate admits even a read-only `viewer` token — the credential meant to
  be handed to a guest — and every accepted socket ran its own push loop over a
  frame that resolves the whole live-tune catalog and reads two slot stores off
  disk. A couple of hundred connections bought a few hundred frame builds a
  second on the host that owns the hardware, stalling the operator's own console
  and every other route on the same app. `/perf/ws` and `/api/ws` now share a
  cap of 8 open sockets and close a handshake past it before accepting, the same
  refuse-rather-than-queue decision the screen stream already made.
