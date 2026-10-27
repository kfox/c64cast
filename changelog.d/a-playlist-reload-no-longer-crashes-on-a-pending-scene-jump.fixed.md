- **A playlist reload could end the show over a pending scene jump.** A jump
  requested just before a reload (SIGHUP or the control plane) kept its index
  into the old scene list; when the reloaded list was shorter, the next scene
  change raised and the playlist stopped. A reload now drops the jump.
