- **Four more cleanup blocks skipped the rest of their work after one step
  failed**, and the class is now closed by a check rather than by another hand
  search. Worst of them was the bitmap scenes' raster-IRQ teardown: six
  independent writes under one swallow, where a single transient link error
  left the C64's IRQ vector pointing into RAM the next scene overwrites, the
  VIC on a non-default bank, and **CIA #1's timer masked — which stops the
  keyboard scan, and with it pause/skip on the C= and CTRL keys, for the rest
  of the session**. The others: the ASID player's kernal-IRQ restore, whose two
  halves share a function precisely because each matters alone, and whose
  second half is the one that keeps the jiffy clock from running 16x fast; DAC
  calibration's teardown, where a failed CIA write skipped both the SID
  silencing and the reset, ending a run with the machine still making noise;
  and the oscilloscope's subtune-change pre-silence, where a failed vector
  restore skipped the silencing it exists to do. Each write is now guarded on
  its own and names itself when it fails — except the raster teardown's last
  step, re-enabling the C64's keyboard timer, which stays deliberately tied to
  the IRQ-vector restore above it: re-arming that timer while the vector still
  points at the departing scene's handler hands every interrupt to RAM the next
  scene overwrites, so a failed vector restore now leaves the timer masked and
  says so. A test sweeps the tree for the shape and fails on a new one, since
  the previous fix in this class recorded that it was the last instance and was
  wrong four times over.
