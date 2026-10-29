- **A `clock` showing seconds and a `countdown` no longer skip a second now and
  then.** The corner text resampled its value one refresh after the last frame
  that did, so the sample point drifted across the second boundary roughly once
  a minute and one value never appeared; it now resamples on whole multiples of
  `refresh_s`.
