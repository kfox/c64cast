- **A failing step in a SID scene's teardown abandoned every restore after
  it.** `AsidScene`, `MidiScene` and `WaveformScene` each sequenced their whole
  teardown — the kernal IRQ vector and CIA #1 latch, silencing every mapped
  chip, the SID-address config restore, and the char-mode `$D018` — inside a
  single `try`. One transient DMA failure part-way through could therefore hand
  the next scene a machine still holding a sounding SID, a hooked IRQ, or a VIC
  left on the bitmap layout the scope had been using. Every guarantee is now
  its own guarded step: a step that fails is logged with its name and the
  remaining restores still run. `AsidScene` also closes its MIDI port before
  the restores rather than after them, so a reader thread abandoned by
  teardown's bounded join cannot read one more speed message and put CIA #1
  back on the stream's rate behind them. The four non-SID scenes with the same
  shape are guarded too: the `webcam` and generative scenes could hand the next
  scene a still-streaming audio pump or a leaked capture handle when the live
  `force_palette` worker failed to stop, the `video` scene could do both (plus
  suppress the next lap's first OSD repaint) when the record-border restore hit
  a link error, and the `launcher` scene could leave a `.crt` cartridge active
  when its input poller failed to join. The three composable audio sources are
  guarded on the same terms, and a failing step now says so: it is reported at
  ERROR with the name of the guarantee that was abandoned, where `AsidScene`
  and `MidiScene` previously logged at DEBUG — one line for the port close and
  one for the whole silence/restore group — and so said nothing at all unless
  you were running with `-v`. `WaveformScene` already reported at ERROR. A
  playlist run against an unreachable U64 is therefore noisier at every scene
  boundary than it was — one record per abandoned restore rather than none —
  which is the intended direction: those restores were being skipped silently
  before. The `sid` source wrapped the KERNAL-vector restore and the chip
  silences in one `try`, so a REST hiccup on the vector left every SID ringing
  until something else happened to write `$D418`. The `file` source could hand
  the next scene a streaming pump when its decode thread failed to start at all
  — on a machine out of threads, the audio output was already up and the failed
  thread was already recorded, so stopping the output was abandoned while
  trying to wait for a thread that had never run. It now records the thread
  only once it is running, so there is nothing to wait for when one cannot
  start. The `mic` and `listen` sources are guarded to match, though nothing
  reaches the raise their analyzer stop can give.
