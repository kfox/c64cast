- **A scene hidden with the `osd.position` pad no longer stays dark for the
  rest of the run.** Performance mode is a per-scene flag re-stamped each lap,
  but the pad's hide was only ever stamped *on* — and the mode is turned back
  off from whichever scene is live then, which is never the scene it was
  hidden from. That scene kept a set flag nothing cleared, so on a looping
  playlist its OSD was dead for good and further taps on the pad did nothing.
  The re-stamp now writes the mode's actual value every lap, and a tap clears
  the run-level gate on the scene in front of it rather than inferring it, so
  the pad really does open whichever gate is shut.
