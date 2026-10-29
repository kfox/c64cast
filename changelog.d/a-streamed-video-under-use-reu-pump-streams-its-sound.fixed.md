- **A video URL or stream no longer stalls setup under `[audio].use_reu_pump`.**
  The REU pump preloads a clip's whole soundtrack before it plays, and a live
  stream has no end: setup waited for as much sound as the REU holds (about
  half an hour at the DAC's rates) to arrive in real time, or for good when the
  stream's sound track sent nothing. A network input now streams its sound as
  it would with the pump off; local files still use the pump.
