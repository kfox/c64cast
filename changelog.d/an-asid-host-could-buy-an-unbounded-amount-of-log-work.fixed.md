- **An ASID host could buy an unbounded amount of log work with one 62-byte
  message.** The MIDI reader's drain is bounded at 64 messages a pass so the
  coalesced register flush and the stop check that ends teardown always run —
  but that bounds *messages*, and the wire picks the *work* per message. One
  WARNING costs ~322 us through the default terminal handler, so 64 of them in
  a pass is 20.6 ms on a loop that is otherwise sub-millisecond, and two
  warnings on the ASID path fired once per message with no gate at all: the
  over-long `0x30` timing recipe (18 MB/s into an unrotated `--log-file` at full
  decode rate) and the ring player's slot-truncation report, which runs at the
  ASID frame rate, 60 to 960 Hz, and can hold for a whole scene rather than a
  frame. Both now report at most once a second per stream — first occurrence at
  WARNING as written, a repeat at DEBUG carrying how many occurrences it stands
  for. One shared implementation rather than a hand-rolled flag apiece, and one
  instance per stream, owned by the scene that reads the port — so two systems
  in an ensemble each keep their own first report instead of whichever one is
  flooded first spending the other's. A drain pass now also releases after a
  quarter of the flush period however cheap the count bound thinks it has
  been. Nothing is dropped that used to be delivered:
  the pass checks its deadline before taking a message off the port, and the
  first message of a pass is never gated. That quarter-period budget is sized
  for the ASID reader, whose per-message cost is microseconds; the MIDI
  instrument scene, which writes to the SID over the link *inside* its drain,
  sizes its own from what a chord of notes costs on the link in use, so a note
  flood still retires a chord a pass rather than one message.
