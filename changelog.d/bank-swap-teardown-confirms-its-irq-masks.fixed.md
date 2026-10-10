- **Leaving a hires or mhires scene over a lossy link no longer risks
  wedging the C64.** The scene's teardown masks both IRQ sources before it
  unhooks the bank-swap handler, and a mask write lost on the link used to go
  unnoticed: an IRQ could then start an REU copy after the teardown's wait,
  which the next scene's setup ran under. The two masks and the `$0314` restore
  are now confirmed delivered and written again when lost; a mask that never
  lands is logged, and the teardown waits out a late copy before it releases
  the VIC bank.
