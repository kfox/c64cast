- The Ultimate 64's own VIC output stream (`vic_stream.py`) accepted a UDP
  datagram from any sender, not just the machine it asked to stream — a
  spoofed or garbled flood could inject fake frames or grow the partial-frame
  reassembly buffer without bound (it only shrank on a silence timeout, never
  on a byte cap). The receiver now checks the packet's source address and
  caps the reassembly buffer independent of that timeout. `start()` also
  leaked its socket if the streaming request failed with a `SocketDMAError`
  rather than a bare `OSError`; both are now caught. `stats` now reads its
  counters under the receiver's lock.
