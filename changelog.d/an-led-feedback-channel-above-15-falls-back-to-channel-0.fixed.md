- **An LED feedback channel above 15 no longer leaves every pad dark.** A
  controller profile's `feedback.channel` accepted any value up to 127, and
  every LED message on a channel above 15 failed silently, so no pad ever lit.
  A channel outside 0-15 now falls back to the default channel 0, and
  `--midi-setup` says so when one is typed instead of saving it.
