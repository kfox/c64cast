- A section's cascade behavior is now spelled out in exactly one place. The
  cascading list and a not-cascading list (each entry with its reason)
  together classify every scalar section plus `[color]` exactly once, and a
  test asserts the partition *and* that every section listed as cascading
  really does receive a master value — the check that would have caught the
  drift above. A master section that reaches nothing (today `[video]` alone)
  is now called out with a warning instead of being dropped in silence.
