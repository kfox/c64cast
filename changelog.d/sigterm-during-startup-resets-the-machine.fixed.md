- **A SIGTERM while c64cast is starting up now resets the machine.** A `kill`
  or a service manager's stop that arrived before the first scene used to end
  the process at once, leaving the machine's video mode, REU, sampler and
  volume settings changed and the machine never reset. It now unwinds the
  startup the same way Ctrl+C does.
