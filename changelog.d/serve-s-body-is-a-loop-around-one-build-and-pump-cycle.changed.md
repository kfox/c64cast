- `--serve`'s body is a loop around one build-and-pump cycle rather than a
  single ~140-line function that also installed signal handlers, resolved
  credentials, decided whether to open the setup window, printed three
  banners, autostarted and owned the shutdown ordering. No behavior change
  beyond the fixes above; each of those decisions is now separately testable,
  which is what the setup-window and shutdown-order regressions needed.
