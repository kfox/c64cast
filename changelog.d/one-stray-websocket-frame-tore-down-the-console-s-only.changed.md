- One stray WebSocket frame tore down the console's only state feed: a text
  frame that is not JSON raises `JSONDecodeError` and a *binary* frame
  raises `KeyError`, neither of which is a disconnect, so both fell through
  to a blanket `except Exception: log.debug(...)` — the socket closed and
  the browser reconnected into the same failure, with nothing in the log at
  default verbosity. Unparseable frames are now ignored and the loop
  survives; an abrupt transport close stays at debug and everything else is
  logged at `exception`, since a socket nobody asked to close is not a debug
  detail.
