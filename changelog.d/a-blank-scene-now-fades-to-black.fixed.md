- **A `blank` scene now fades to black.** The blank canvas is its background
  color, and the scene fade and the WLED brightness slider dimmed only the
  invisible character colors in front of it, so a blank title card with a
  colored `background` or `border` stayed at full brightness through
  `[playlist].fade_duration_s` and then cut. Both registers now dim with the
  rest of the frame, and a blank scene fades in from black instead of
  opening on its full colors.
