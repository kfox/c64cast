- `--calibrate-dac` opened the backend before the try/finally that closes
  it, so `hw_provision.resolve_system` — which talks to the machine to
  settle `system = "auto"` — raising on an unreachable/unresponsive C64
  abandoned the backend's persistent DMA socket; the U64 DMA service is
  single-connection and blocks new sockets for seconds after an unclean
  close, so the operator's very next attempt failed too, looking like an
  unrelated problem. The resolve call now runs inside the same try/finally
  that already closes the backend.
