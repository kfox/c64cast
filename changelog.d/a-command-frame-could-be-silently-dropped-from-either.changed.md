- A command frame could be silently dropped from either console WebSocket.
  Both push loops wrapped `receive_json()` in `asyncio.wait_for`, which
  *cancels* the receive every 0.35 s — and a frame delivered in the same
  event-loop turn as the timeout is popped off the queue and then thrown
  `CancelledError`, so it was consumed and never acted on, with `except
  TimeoutError: continue` making the loss invisible. A pad tap or a
  `{"session": "stop"}` on a host that owns live hardware simply did
  nothing. The receive is now a long-lived task that survives a timeout.
