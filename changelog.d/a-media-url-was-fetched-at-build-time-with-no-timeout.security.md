- **A media URL was fetched at build time with no timeout.** The yt-dlp
  resolution runs inside `build_scene`, i.e. after the link is open and the
  machine has been reset, and it passed no `socket_timeout` (nothing in the tree
  calls `socket.setdefaulttimeout` either). A host that completed the TCP
  handshake and then never answered held the C64 in reset with the DMA socket
  open until the process was killed — and under `--serve` the supervisor stayed
  `STARTING`, so `POST /api/session/stop` returned 202 and changed nothing. The
  attacker is whoever controls the host behind a pasted link, which is a normal
  VJ workflow. It is bounded now, one `log.info` line names the URL before the
  fetch (yt-dlp's own logger goes to `log.debug`, so there was nothing at
  default level saying what it was waiting on), a resolved stream URL whose
  scheme is not http/https is refused rather than handed to ffmpeg — which
  honors `file://` and `udp://` — and the resolved title is stripped of control
  characters and length-capped before it becomes the scene name, since it comes
  from the page and lands in log lines interpolated with no arguments.
