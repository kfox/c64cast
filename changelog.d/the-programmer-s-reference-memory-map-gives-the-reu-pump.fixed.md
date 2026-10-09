- **The Programmer's Reference memory map gives the REU pump's tracker its
  full size.** It listed `$C200` as a three-byte tracker; the tracker is five
  bytes (`$C200-$C204`) and the pump's tick counter follows at `$C205`, so data
  placed at `$C203` from the old table would land on the pump's write head.
