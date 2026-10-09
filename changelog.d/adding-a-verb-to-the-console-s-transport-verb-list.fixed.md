- Adding a verb to the console's transport verb list without adding a dispatch
  branch would have silently saved or cleared one of the performer's persisted
  loop presets: the branch chain ended in an unconditional `loop_slot` enqueue
  with no `if`. The last branch is explicit and an unhandled verb is refused,
  with a test that walks the list and asserts each verb has its own effect.
