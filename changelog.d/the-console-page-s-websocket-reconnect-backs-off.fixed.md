- The console page's WebSocket reconnect backs off exponentially (0.5 s to 15 s)
  instead of retrying at a fixed interval forever, and now retries at all after
  a construction failure, where it used to fall back to polling and never try
  the socket again for the life of the page. The WLED device page's copy of the
  same loop gets the same fix — the two had already drifted to different delays
  with no backoff on either.
