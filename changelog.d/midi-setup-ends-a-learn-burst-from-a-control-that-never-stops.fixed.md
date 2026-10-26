- **`--midi-setup` no longer waits forever on a control that never stops
  sending.** A jittery fader or an expression pedal that kept sending
  messages held the learn step open, because it waited for a pause in input
  that never came. A learn burst now ends at most five seconds after its first
  message.
