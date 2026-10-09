- **`-vv` no longer shows the reads c64cast makes on a timer, and `-vvv` is
  new.** The Commodore-key poll reads the machine ten times a second for the
  whole run, so a five-minute session buried `-vv` under ~3,000 HTTP-transport
  lines that said only that the poll was still polling — and `--log-file` grew
  at that rate. Those reads, the launcher scene's idle detector and the
  host-DMA audio servo's ring-pointer read are now held out of `-vv`, which
  leaves it showing the requests an operator is actually asking about. A
  warning raised during one of those reads is not held back: a retry says
  something about the link, which is the whole question. `-vvv` puts the poll
  traffic back, for the run where the poll itself is the suspect — a C= hold
  that never resumes, a launcher scene that never goes idle.
