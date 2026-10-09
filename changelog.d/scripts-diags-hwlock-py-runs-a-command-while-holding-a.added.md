- **`scripts/diags/hw_lock.py` runs a command while holding a per-user lock on
  one rig**, so the shells and agents one user account runs against a rig take
  turns on the U64's single-connection DMA service and the capture device instead of
  breaking each other's runs. It waits, says on stderr who holds the lock, then
  execs the command, so the exit code and Ctrl-C are the command's own.
  `--device` keys the lock on a URL's host, so a second rig does not wait on the
  first. POSIX only.
