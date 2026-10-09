- **An ARM2SID's second SID now plays two-SID tunes and ASID streams on an
  Ultimate 64.** The firmware reports the chip as an ARMSID and socket 2 as
  empty, so the right channel was never used. c64cast now asks the chip itself,
  routes the tune's `$D420` chip to the right channel by setting `Ext DualSID
  Range Split` to `A5` for the scene, and sets each channel's model separately.
  The resolved-audio line names it as `socket2 (ARM2SID R 8580)`.
