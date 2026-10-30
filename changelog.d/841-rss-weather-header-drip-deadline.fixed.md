- **`rss` and `weather` give up on a server that sends its response a byte at
  a time before the body starts.** The 20-second limit used to begin only
  once the headers had arrived, so a server dripping its TLS handshake, status
  line or headers held the fetch indefinitely; the limit now covers the whole
  exchange.
