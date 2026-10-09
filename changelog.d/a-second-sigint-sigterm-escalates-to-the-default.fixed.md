- A second SIGINT/SIGTERM escalates to the default disposition per signal, as
  documented. One shared flag meant the first SIGTERM after a Ctrl+C took the
  escalation branch — arming the hard kill a signal earlier than promised, and
  dropping that SIGTERM's own stop request.
