- **A `.sid` tune on an Ultimate 64 with an ARMSID now plays on the ARMSID,
  switched to the model the tune asks for.** The chip's model is a setting, but
  SID autoconfig compared the socket's `ARMSID` label against `6581`/`8580`,
  never matched, and moved every tune to an UltiSID core with the socket
  disabled. The model is restored when the scene ends.
