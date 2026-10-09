- **`--doctor` no longer warns "REST query for SID status failed" on an
  Ultimate 64 running firmware 3.15.** Firmware 3.15 answers a read of a
  config category the device does not have with HTTP 404, where earlier
  firmware (and the C64 Ultimate's 1.1.0) answered 200 with an empty body.
  c64cast now reads both answers as "this device has no such category", and
  the emulated-SID check runs only on a device that has emulated SIDs (the
  Ultimate II family). On 3.15 the Ultimate Audio sampler check likewise
  reported that the sampler's state "could not be read" on a device with no
  sampler mixer; it now reads the sampler as absent and falls back to the
  4-bit DAC without that warning, as it did before 3.15.
