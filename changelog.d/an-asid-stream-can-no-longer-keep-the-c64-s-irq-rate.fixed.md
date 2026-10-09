- **An ASID stream can no longer keep the C64's IRQ rate after its scene ends.**
  The buffered ring player programs CIA #1 Timer A the moment it installs but
  hooks `$0314` only once a real-frame prebuffer arrives — and teardown restored
  the kernal latch only when it had reached that second step. A stream that sent
  one `0x31` speed message and no register frames at all therefore left Timer A
  at whatever rate it asked for: at the band ceiling that is a jiffy clock 16x
  fast and a third of the machine's cycles spent in `$EA31`, for every scene
  after it, until a power cycle. Teardown now restores the vector and the latch
  whenever the player touched the machine at all, and the scene repeats the
  restore itself rather than trusting the player's bookkeeping.
