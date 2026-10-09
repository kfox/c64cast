- **With `[audio].nmi_rate_adaptive` on, a link stall no longer speeds the
  `$D418` DAC up.** After a stall of about a second, the consumer-rate
  estimate saw only the part of the read pointer's advance past a whole ring
  lap, read the consumer as several times too slow, and stepped the NMI up by
  as much as 5%. A reading across an interval that long is now dropped.
