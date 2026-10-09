- **A stop pressed during a show switch was discarded, and the switch
  started the new show anyway.** `stop()` consulted only the supervisor's
  state, so for the whole duration of a `switch` an operator's stop was
  refused as "not running" during the teardown and dropped in the idle gap
  before the replacement was claimed — answered `202` by the route either
  way. A stop landing anywhere inside a switch now cancels the pending
  start.
