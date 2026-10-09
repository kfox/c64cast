- **An Ultimate with a network password now accepts c64cast's REST calls as
  well as its DMA writes.** The firmware checks the same password on its REST
  API, in an `X-Password` header, and c64cast only ever sent it on the DMA
  socket. So with a password set the screen painted, but every REST call was
  refused with 403: machine reset, program and SID player launch, keyboard
  reads, and every config read and write (REU and sampler provisioning, SID
  routing, `--doctor`'s checks). `C64CAST_DMA_PASSWORD` and
  `[ultimate64].dma_password` now reach both links, so nothing in your setup
  changes. Startup and `--doctor` also now say so when the REST API refuses
  the password, instead of failing piecemeal later, and a password containing
  a control character or a leading or trailing space or tab, which an HTTP
  header cannot carry, is refused at startup (and by `--doctor --skip-probe`)
  without being echoed. `--dump-char-rom` and `--calibrate-dac` now exit 4
  with that message, or a refused DMA password or TeensyROM link error,
  instead of a traceback.
