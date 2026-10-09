- **A `.sid` header can no longer declare a SID chip on top of the REU.** The
  PSID spec permits an extra chip anywhere in `$DE00-$DFE0`, and `$DF00` is
  where c64cast drives its own REU: a 25-byte teardown write there lands on
  the command registers, two of which the audio ring's interrupt handler reads
  back mid-transfer as its destination pointer. Bases whose register window
  reaches the REU are refused and the tune degrades to single-SID.
