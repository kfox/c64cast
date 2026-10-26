- **Stopping a show while a MIDI controller floods the control or clock port
  now stops the MIDI readers.** Each reader drained every waiting message
  before checking whether to stop, so a controller sending faster than the
  reader kept up held it in that drain past teardown. The readers now take a
  bounded batch per pass and check for a stop between messages.
