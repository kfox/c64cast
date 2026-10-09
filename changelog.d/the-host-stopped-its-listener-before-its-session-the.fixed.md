- **The host stopped its listener before its session, the reverse of the
  documented order.** On a shutdown signal the mDNS record and the HTTP
  server went down first and the session was torn down only after the loop
  returned — so every connected console lost its socket and *then* waited out
  up to a minute of hardware teardown it could no longer watch, which is
  precisely the failure the docstring and the architecture note both said was
  avoided. The session now comes down first on the shutdown path (the
  restart path still replaces only the listener), and a test pins the order
  rather than leaving it to prose.
