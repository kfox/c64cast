- **A TeensyROM+ command that fails part way no longer throws the link out of
  step.** A write whose send was cut short by a link error left the firmware
  waiting for the rest of it, so the next command's bytes landed in C64 memory
  as that write's payload, and a reply that arrived after the client gave up
  on it was read by the next command, leaving every later read a reply behind.
  The command after any such failure now waits for the firmware to give up and
  finish replying first: about 2 s when it follows the failure at once, and
  well under a second on a link that has stopped answering.
