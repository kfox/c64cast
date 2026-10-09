- **A lossy link that kept the REU audio pump's `$0314` from being restored no
  longer leaves the C64's clock and cursor running slow.** Until a later scene
  landed the restore, every timer interrupt still went to the pump's entry,
  which hands off to the KERNAL only every third tick. That entry is now
  replaced by a jump straight to the KERNAL.
