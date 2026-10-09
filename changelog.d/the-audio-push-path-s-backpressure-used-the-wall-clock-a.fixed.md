- **The audio push path's backpressure used the wall clock.** A clock step
  during a run either expired the producer's 200 ms wait instantly, dropping a
  blob that had capacity coming, or parked the decoder thread for the length of
  a backward step; every other deadline in the module is monotonic. A blob
  larger than the whole queue cap could also never satisfy the gate however
  empty the queue got, so it was dropped forever with no diagnostic — an empty
  queue admits it once now.
