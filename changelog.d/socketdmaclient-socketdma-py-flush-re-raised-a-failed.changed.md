- `SocketDMAClient` (`socket_dma.py`): `flush()` re-raised a failed IDENTIFY
  round-trip (including a `TimeoutError`, an `OSError` subclass) without
  closing the socket, so the *next* command read that reply's stale length
  byte and payload as its own — permanently one reply behind on the sync
  barrier every REST runner call (`run_prg`/reset) depends on. Both
  handshake steps (`_authenticate_locked`, `_identify_locked`) sent their
  command before entering the try block that maps failures to
  `SocketDMAError`, so a peer that reset the connection mid-handshake let a
  raw `OSError` escape past `connect()`'s documented contract, skipping
  callers' cleanup (`session._open_backend`'s camera release,
  `doctor._probe_connectivity`'s "one dead system doesn't hide the others").
  `_recv_exact_locked` restarted its 2s socket timeout on every chunk
  instead of tracking one cumulative deadline, so a peer that dribbled a
  reply back one byte at a time could wedge the read (and the lock this
  client shares with `AudioStreamer`) far longer than `io_timeout`. A
  command payload over 65535 bytes raised a bare `struct.error` instead of
  `SocketDMAError`, escaping both the reconnect handling and the caller's
  documented error contract. `keyb()`'s docstring claimed the firmware
  clamps to the 10-byte kernal buffer; it doesn't (verified against
  socket_dma.cc) — a longer write reaches past $0277 into $0291 and beyond,
  so the bound is now enforced client-side. `vicstream_on`'s watchdog
  encoding rounded any `stop_after_s` under 2.5ms — and any negative
  value — onto the sentinel its own docstring defines as *unbounded*, the
  exact inverse of the request; sub-tick durations now round up to one
  tick and a negative duration raises. A rejected password kept being
  re-dialed and re-offered in cleartext on every subsequent write with no
  backoff; it's now sticky until an explicit `connect()`, and `close()` is
  now similarly terminal (a write after it raises instead of silently
  reopening a connection nobody owns). The retried command in
  `_send_with_reconnect` left the socket assigned when it also failed,
  risking a misframed next command; both that path and the transient-
  retry's own log line (previously an unconditional `WARNING`, duplicating
  what `backend.py`'s escalating failure ladder already reports on a
  sustained outage) are fixed. `latency_summary()`'s percentile index was
  one rank high for small windows (p95 printed identical to max at n=20).
  The device-supplied IDENTIFY string is now filtered to printable
  characters and capped at 64 bytes before being logged, closing off a
  hostile or misconfigured peer forging lines into `--log-file` output.
