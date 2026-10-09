- Two test-isolation defects found while fixing the above, both reaching the
  developer's real `~/.local/share/c64cast/`: one supervisor test wrote and then
  **deleted** the real run marker (the file that tells a `--serve` host its last
  session did not shut down cleanly), and the live-tune tests read the real
  character ROM and cached it process-wide for every other test in the worker.
