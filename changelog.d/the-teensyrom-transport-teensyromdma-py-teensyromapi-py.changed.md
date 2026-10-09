- The TeensyROM+ transport (`teensyrom_dma.py`/`teensyrom_api.py`):
  `_settle_after_launch` read `self.tr.transport` directly after
  `launch_file()` had already released `TRClient`'s lock, unsynchronized
  against the keyboard/menu poller's concurrent `read_memory` calls on the
  same link — exactly the window the documented, still-open launcher-upload
  race lives in. It now goes through a new locked `TRClient.
  drain_after_command`, and the class docstring is scoped to state plainly
  that the per-command lock doesn't cover this asynchronous post-ack
  chatter. `delete_file`/`post_file`/`launch_file` raised a bare
  `UnicodeEncodeError` for a non-ASCII path instead of the `TRError` family
  every caller in this module is documented to expect, and didn't reject an
  embedded NUL (which would truncate the device's parse early and desync
  the following command's framing) — both are now validated before
  framing. `SerialTransport.drain_text` never overrode the fixed 2s
  `io_timeout` on its underlying `read()`, so the common "nothing to
  drain" case paid a full io_timeout stall instead of returning after its
  own `quiet_s`; it now does, restoring the prior timeout afterward.
  `SerialTransport.recv_exact`'s overall deadline was only checked when a
  read returned nothing, so a link that trickled in at least one byte per
  call never tripped it and could hold `TRClient._lock` indefinitely;
  `TcpTransport.recv_exact` tracked no deadline at all, the same gap with
  no partial mitigation. Both now check a cumulative deadline every
  iteration. Both transports' `drain_text` also had no hard ceiling on
  total time or bytes independent of the quiet-window reset, so a
  misbehaving or hostile device that never quite goes idle could hold the
  drain — and the lock every write/read command needs — open indefinitely;
  both now bail (logged) past a fixed wall-clock/byte cap. Device-supplied
  text (NAK reasons, the Ping status line, the Reset response line) is now
  filtered to printable characters at the transport boundary before it can
  reach a log record or an exception message. `probe()` treated a fully
  empty Ping reply (which `drain_text` returns instead of raising, even
  when the TR is hung or disconnected) the same as a successful-but-blank
  reply, fabricating a "TeensyROM (... firmware)" liveness string for a
  device that sent zero bytes back; it now returns `None`, matching its own
  documented contract. `describe_device` reached past `TRClient` into the
  concrete `SerialTransport` class via `isinstance`; `TRTransport` now
  exposes an optional `serial_number` property (`None` by default) that
  `describe_device` reads polymorphically instead. Two findings from this
  pass are documented rather than fixed, for lack of a way to fix them from
  this client: the wire protocol has no ReadFile-from-storage token, so
  there is no host-side readback/hash check that could run between
  `post_file()` and `launch_file()` beyond the checksum `post_file` already
  verifies device-side; and `_upload`'s pre-delete swallows every delete
  failure, not only "file doesn't exist", because the firmware's FailToken
  carries only free text with no known, stable "not found" pattern to
  narrow the catch against.
