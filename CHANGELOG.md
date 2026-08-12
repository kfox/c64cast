# Changelog

All notable changes to c64cast are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and c64cast follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) over its *user*
surface — the CLI flags, the config schema, the `example:` names, and the data
directory layout. The Python API carries no stability promise while the version
is `0.x`.

Work lands under `## [Unreleased]`; cutting a release renames that section to
the version and stamps it with the date.

## [Unreleased]

### Added

- **A multi-SID tune on a single-SID machine now says so.** On a link that
  can't route chips (TeensyROM+, Ultimate II+), a tune driving an address past
  `$D400` that no `[hardware].host_sid_chips` entry covers gets a warning once
  per run, naming the address and both readings of it: a multi-SID tune picked
  by mistake, or a dual-SID mod that hasn't been declared. Previously only a
  machine that *had* declared its chips was told — which is backwards, since
  the default configuration declares nothing and is where a mistaken pick is
  likeliest to land. On an Ultimate II+ the warning points at the Ultimate's
  own audio jack, where the tune does play as authored.

- **`[hardware].host_sid_tune_match` picks tunes your C64's own SID chips can
  play.** When a `waveform` scene points at a directory or glob, `"prefer"`
  tries tunes that fit the chips you've declared before the rest — the right
  model, and a chip at every address the tune drives — so a single-SID machine
  stops landing on 2SID tunes whose second voice-set goes nowhere, and a 6581
  machine stops landing on tunes composed on an 8580. `"require"` drops the
  misfits outright. Both fall back to the whole pool (with a warning) when
  nothing fits, so a mistyped chip table shows up in the log instead of as a
  scene that never starts. Default `"off"`. It never acts on the NTSC/PAL
  guess: declare `host_sid_chips`, or set `host_sid_model` explicitly, to turn
  it on. Only `host_sid_chips` can skip a 2SID tune — `host_sid_model` names one
  chip without claiming it is the only one.

- **`[hardware].host_sid_chips` describes a machine with an internal dual-SID
  mod.** A C64 fitted with an ARM2SID, SIDFX or DualSID answers at a second
  address in its own hardware, often at the other chip model — much of the
  reason for fitting one. Such tunes already played correctly on those machines
  and still do, with nothing routed: the tune writes to both addresses and the
  chips are already there. What was wrong was what c64cast *said* about it, on
  links that can't read the SID hardware state — it assumed one chip and
  reported the second as inaudible while you were listening to it. Declare the
  chips (`host_sid_chips = { d400 = "6581", d420 = "8580" }`) and each one gets
  its own verdict against the tune. The declaration supersedes
  `host_sid_model`, so the NTSC/PAL guess and its warning drop away with it.

- **When the two audio outputs disagree, c64cast now says which one to listen
  to.** Matching a tune to an 8580 emulation is the right move on a machine
  whose internal chip is a 6581 — but the tune then plays on that unchanged
  6581 through the AV cable, sounding thin and scratchy while every line in the
  log reports a match. That reads like a dying SID, and someone can lose an
  evening to it before suspecting the cable. The mismatch was already reported;
  it is now accompanied, once per run, by what it means and what to do about
  it. Not emitted when the emulations are wrong too — then the problem really
  is configuration, and pointing at a cable would misdirect.

- **A tune loading into the RAM under `$D400-$D7FF` now warns on an Ultimate
  II+.** Its emulated SIDs take writes off the cartridge port, which carries no
  signal separating an I/O access from one to the RAM below — so a tune living
  there is heard as register writes, and arrives as clicks and stray notes on
  the Ultimate's audio output. A warning, never a refusal: the tune plays
  correctly, and the C64's own output is fed by real chips that decode
  properly.

- **`sid_model` now matches chip models on the Ultimate II+ too, instead of
  only reporting them.** The resolved-audio line could already tell you a tune
  had asked for an 8580 and was playing on a 6581 emulation — and nothing could
  act on it, because model matching existed only for the U64's sockets and
  UltiSID cores. The U2+ needs none of that machinery: its audio jack is fed by
  two SID emulations, so the side already snooping a tune's chip is simply told
  which model to be. `Filter Curve` and `Combined Waveforms` move together,
  since a side split between the two emulates neither chip, and your settings
  come back at teardown. The host C64's own SID still plays the tune unmatched
  on the machine's own output — nothing can change which model that internal
  chip is — so a mismatch there is still reported rather than papered over.

- **An undeclared host SID model now warns instead of mentioning it.** On a
  link where the machine's own chip can't be read, every model verdict rests on
  the NTSC=6581 / PAL=8580 convention — a rule that is frequently wrong, since
  NTSC machines carrying an 8580 are common. That guess used to be stated at
  INFO, where it scrolled past between the lines that depended on it; it is now
  a once-per-run warning naming the field that settles it. Declaring
  `[hardware].host_sid_model` silences it for good, and `"unknown"` opts out of
  host-chip verdicts entirely.

- **Video scenes now draw a buffering bar on the C64 while they load.** A
  diagonal-striped bar grows along screen row 22 through the blocking setup
  work (container open, color pre-scan, audio encode, REU upload) in every
  display mode — no text or numbers, the right edge is 100%, and the first
  video frame wipes it. `[video].setup_progress_bar = false` turns it off.

- **`--calibrate-dac` now says so on the C64 itself.** The machine used to sit
  on a blank screen for the whole ~50 s-per-socket run; it now shows a
  centered title plus a computed duration line (e.g. `MEASURING 2 SIDS -
  ABOUT 90 SECONDS`). Both lines are painted before the first capture and the
  screen is never touched again — mid-run screen DMA could drop NMI samples
  and skew the measurement.

### Fixed

- **Video scenes on the Ultimate Audio sampler path no longer stall ~2 seconds
  at startup.** `VideoScene` started the sampler's blocking prebuffer collection
  before starting the demuxer that feeds it, so every video began by waiting
  out the full prebuffer timeout on silence. The demuxer now starts first —
  the same ordering the audio-file path has always documented and used.

- **`--calibrate-dac` no longer logs 404 tracebacks while isolating the
  mixer.** The per-source volume list spans both config surfaces (U2+
  `EmuSid` vs U64 `UltiSid`), and the isolation step blind-PUT all of them —
  so every U64 run dumped two "Not Found" tracebacks per socket into the
  `-vv` log. Isolation now only touches the items the machine's own mixer
  snapshot reported; a write that still fails aborts the run instead of
  silently measuring a half-isolated mixer.

- **An Ultimate II+ now says up front that it has no SID config surface,
  instead of planning against config that isn't there.** The U64's SID
  routing / chip-model / mixer configuration lives in three REST categories
  the U2+ doesn't have — and the firmware answers queries for missing
  categories with an empty success, so every SID-playing scene silently read
  empty state and planned against it. Connecting now probes the device's
  actual category list once (after reachability is already proven; `--skip-probe`
  costs nothing new) and logs a single line when the surface is absent; SID
  tunes then play on whatever answers their addresses, with the model verdict
  coming from `[hardware].host_sid_model`. Capability detection is by config
  category presence, not the product name, so it tracks firmware differences
  within one product too.

- **The Ultimate II+'s Ultimate Audio sampler is detected again.** The sampler
  probe read the mixer volumes from the U64's `Audio Mixer` config category; the
  U2+ carries the same `Vol Sampler L/R` fields in `Audio Output Settings`, and
  its firmware answers a query for a category it doesn't have with an empty
  success rather than an error — so the probe concluded the sampler was absent
  and silently downgraded video audio to the 4-bit `$D418` DAC even with the
  sampler mapped and audible. The probe now searches both categories and every
  mixer write (including the teardown restore) follows the one the device
  actually carries.

- **A tune routed onto an UltiSID core to match its chip model is now actually
  audible.** SID Player Autoconfig pointed a core at the chip's address and set
  its filter curve, but left `Auto Address Mirroring` on and left the physical
  socket enabled at that same address — so the socket's real chip kept answering,
  the mixer pass unmuted *it* and muted the core, and an 8580-tagged tune played
  on a 6581 while the log said it had been routed to an FPGA core. Nothing
  errored: every config write succeeded. The UltiSID fallback now disables
  mirroring and the socket it displaces (both already covered by the
  snapshot/restore, so your config comes back at teardown). Verified on hardware.

### Added

- **The Ultimate II+'s emulated stereo SIDs are now routed, panned, and
  leveled like the U64 mixer.** The U2+'s audio jack carries two FPGA SID
  emulations, each snooping one configurable bus address — and the stock
  right-side base is not `$D420`, so a 2SID tune played half-silent with no
  error anywhere (the host C64's own output can't help: a real SID answers
  the whole `$D4xx-$D7xx` range, so multi-SID tunes collapse onto one chip
  there). SID-playing scenes now retarget a spare *enabled* side to any
  uncovered chip address, apply `sid_panning` / `sid_volume` to
  `Pan/Vol EmuSid1/2`, and restore the user's config at teardown — a side
  that was disabled is never touched. The resolved-audio line reads the same
  surface back (`$D400 → emusid1 (6581) @ 0 dB Left 3`), with the declared
  host-SID verdict appended, since the machine's own SID still plays the tune
  on its own output. Teardown silences every routed chip *before* putting the
  snoop bases back: the emulation's voice state survives a machine reset
  (hardware-verified), so a side moved home mid-note would otherwise keep
  droning where no write could ever reach it.

- **Extra SID chips are now silenced at scene teardown.** Waveform and
  SID-audio scenes only silenced `$D400`; a multi-SID tune's other chips kept
  whatever note was sounding when the scene ended — inaudible in practice on
  a U64 only because the mixer restore usually muted them or the app's exit
  reset cleared the real chips. Every tune chip is now zeroed at the address
  it played, in the same teardown step ASID scenes already had.

- **`[hardware].host_sid_model`** — declare the SID chip model in the C64 being
  driven (`auto` | `6581` | `8580` | `unknown`). On links that can't read the
  SID hardware state (TeensyROM has no config API), the resolved-audio line can
  now still warn when a tune asks for the other model — previously it was
  skipped entirely there. `auto` (the default) assumes 6581 on NTSC / 8580 on
  PAL and logs that assumption once per run; `unknown` opts out. Ignored where
  the live SID state is readable (Ultimate 64).

- **One log line saying what you will actually hear.** After SID routing, model
  matching, panning and volume have settled, c64cast reads the hardware back and
  reports the source answering each of the tune's chip addresses, the chip model
  that source presents, and its mixer level and pan — plus anything else still
  audible that the tune isn't using. A chip that ends up unmapped, muted, or on a
  model the tune didn't ask for makes the line a warning. Until now every step
  logged its *intent* and none logged the outcome, so a chip that was configured
  but inaudible looked identical in the log to one that was playing.

- **The connect-time log now identifies the device, not just its address.** Runs
  report the unit's model, serial and firmware — `Ultimate II+ 5D327C (firmware
  3.14d, FPGA 122)`, or a TeensyROM+'s USB serial number — because an IP or
  serial path names an endpoint, not a machine: two devices can trade addresses
  between runs, and `192.168.2.64` is the Ultimate's factory default that any
  number of units answer to. It also makes a U64-versus-U2+ mismatch legible from
  a log alone, which a bare HTTP 404 against a config URL is not.

## [0.3.0] - 2026-08-09

### Added

- **A TeensyROM+ inside an Ultimate needs `Bus Operation Mode = Writes`, and the
  documentation now says so.** On the firmware's `Quiet` default the pairing may
  play with a constant hiss under the audio, which reads as a c64cast audio
  problem and is not one — the fix is F2 → Cartridge and ROM Settings on the
  Ultimate, and no `[audio]` or `[dsp]` knob substitutes for it. c64cast can't
  provision this the way it does the REU and the sampler, because on that rig the
  connection is `tr://` to the TeensyROM+ and there is no link to the Ultimate
  whose setting it is. Documented in the User's Guide setup chapter, the
  reference guide's DAC section, `caveats.md` and `troubleshooting.md`.

- **`--calibrate-dac` now says which *kind* of unsteady a refused ring was.** A
  spread number alone cannot distinguish a capture whose level was still settling
  — where the ring replayed faithfully and only the level moved — from laps that
  genuinely play different levels, and the two have opposite fixes. Fitting one
  gain per pass separates them, and the failure now picks its cause list from
  that instead of listing everything: a drifting level is no longer blamed on a
  second SID that cannot have caused it. Marginal rings say which kind they are
  as they are measured.

- **A run whose rings are individually fine but collectively marginal now says
  so.** Rings sitting under the trust gate still add up: measured on one chip an
  hour apart, a run with a single marginal ring reproduced at corr 0.9994 / 0.78%
  RMS where a run with none managed corr 1.0000 / 0.00%. The table is still
  written; the run now reports how many rings cleared the healthy band and the
  worst of them, and the count is persisted with the metrics.

- **Audio worker health lines.** Under `-v`, the DAC path now logs a short line
  every few seconds — ring gap excursion, late ring sub-writes, underruns, write
  rate, consumer rate, NMI latch — and reports the session's late-write share on
  stop. The counts that already existed were session totals, which cannot tell a
  fault that is present throughout from one that appears part-way in, deepens,
  clears and returns; the artifacts worth chasing on this path are exactly the
  latter. "Late" means a ring sub-write reached its slot after that slot's
  deadline had passed, which bunches the rest of the chunk and undoes the spread
  described below, without registering as an underrun.

- `scripts/diags/audio_fm_probe.py` — measures how much a host DMA write
  perturbs DAC playback, as a function of payload size, against a tone that
  cannot underrun. Its siblings from the same investigations ship alongside
  it: `halt_shape_probe.py` (what a DMA write costs the 6510, in NMI ticks),
  `ring_race_probe.py` (the write head against the NMI consumer's read
  pointer), `write_delivery_lag.py` (how much of what the host believes it
  wrote is actually in C64 RAM), and `tr_clearloop_state_probe.py` (what the
  C64 is doing at each step of TeensyROM+ bring-up).

### Changed

- **Frames no longer stall on scene cuts on the Ultimate.** The delta-upload
  path decided how to split a changed region into writes by counting bytes,
  which is the wrong currency on socket DMA: a write there costs ~5.2 ms
  regardless of payload up to ~2.4 KB, so splitting a region into pieces
  multiplied its cost while the byte count went down. It now prices both
  options against a per-backend measured cost model and picks the cheaper.
  On a high-motion mhires clip the mean frame went from 18.1 ms to 13.3 ms and
  the worst frame from 104.4 ms to 26.0 ms — the visible hitch on scene cuts,
  where a wide sparse change is most likely. Wide changes also now push only
  the range that actually changed instead of re-uploading the whole region.
  Nothing to configure. TeensyROM+ playback is unaffected: that link *is*
  byte-bound, and the same model keeps the existing behaviour there.

- **The baked `mahoney_ultisid` table has been re-measured** against the
  emulated core in isolation. It reproduces the original curve almost exactly,
  but the code-selection step has been rewritten since that table was generated,
  and the shipped bytes had gone stale against it — non-monotonic through the
  very curve they came from. No action needed; the improvement is automatic for
  anyone whose `$D400` is an UltiSID core.

- **The 4-bit `$D418` DAC wobbles far less.** Each host write to the audio ring
  halts the C64's CPU for about one cycle per byte, and CIA #2 latches NMIs on an
  edge — so a write long enough to span two timer underflows makes the second
  sample vanish rather than merely arrive late. The ring write was one 1024-byte
  push, which at the 12 kHz default froze the CPU for roughly 12 NMI periods
  about 12 times a second, right in the 4-20 Hz band the ear is most sensitive to.
  It is now split into pieces that each fit inside one NMI period and spread
  across the chunk period. Measured on hardware against a 376 Hz carrier,
  frequency deviation drops from 27.3 Hz to about 6 Hz on both the Ultimate 64
  and TeensyROM+. There is no knob: the piece size is derived from the live NMI
  period and floored by what the link can carry, so a backend that cannot afford
  to split degrades to a single write on its own.

- `[audio].dac_calibration_profile` now also takes a **path** to a calibration
  file, used as given. A name is folded into one filesystem-safe token, so a path
  handed to it silently became a key matching no file — and a name can only ever
  address the current backend's own key space, which made it impossible to point
  a TeensyROM+ run at the calibration of the C64 it is plugged into (filed under
  the Ultimate's device id). Missing calibrations now name the file that was
  looked for instead of a mangled key.

- The `vision` extra's mediapipe pin is now `>=0.10.35,<1.1` (previously
  `<0.11`), so a fresh `c64cast[vision]` or `c64cast[all]` install resolves
  mediapipe 1.x. Existing 0.10.x installs remain within the pin; the
  hand-gesture controller works with either series.

### Fixed

- **Ctrl+C now always stops a headless run, and always tears down.** With no
  `[preview]` window the main thread waited on a plain `Thread.join()`, which
  CPython 3.14 parks in `_PyParkingLot_Park` — a wait no signal interrupts. The
  main thread therefore never returned to the interpreter, Python never ran a
  signal handler, and Ctrl+C did nothing at all: measured on a hung run, two
  SIGINTs produced no shutdown, no teardown and no final reset, leaving the
  machine mid-session. SIGTERM was equally stuck, so there was no graceful way
  out. Only runs with a preview window escaped, because pumping a window polls
  `is_alive()` anyway — which is why Ctrl+C seemed to work only sometimes. The
  headless join now polls, and SIGINT is handled explicitly alongside SIGTERM
  instead of riding `KeyboardInterrupt`, so an interrupt can no longer land
  inside teardown and abandon the run's final reset. A second Ctrl+C restores
  the default handler rather than exiting on the spot, so the third is what
  kills — cutting an in-flight DMA is what wedges the hardware.

- **Nothing writes to BLNSW (`$00CC`) any more.** Poking `$80` there to stop the
  kernal cursor blink never worked — the editor's input-wait loop overwrites that
  byte on every pass, so it is gone microseconds after the DMA lands. The BASIC
  clear-and-loop PRG is the mechanism that actually holds the cursor off, and it
  was already doing so everywhere the write claimed to help. The
  `suppress_cursor_blink()` helper is gone, and the TeensyROM+'s "is BASIC at the
  READY prompt?" check — which had been reading that state as a side effect of
  the same write — is now a plain read of CURLIN.

- **TeensyROM+ bring-up now actually detects the READY prompt.** The check for
  "is BASIC at the READY prompt, so the clear-loop repair needs to run?" tested
  CURLIN's high byte for `$FF`, the usual shorthand for direct mode. Measured on
  hardware that machine reads `$0000` at READY and `$0014` (line 20) running the
  clear loop, so the test matched neither state, every probe answered "running",
  and the repair never fired — leaving BASIC in the editor with a blinking
  cursor. Both spellings of "not executing a line" are now accepted.

- **TeensyROM+ bring-up no longer skips the clear-loop repair after a slow
  launch.** LaunchFile acks and then streams its own console text back over the
  same link, a C64 reset included. Bring-up waited a fixed 0.6 s for that and
  then probed, which on hardware was short: the probe's reply misaligned with the
  text and came back as the ASCII of `Remote Launch:`, so the state read failed,
  the repair took its can't-read-the-state early return, and BASIC was left in
  the editor — with exactly the blinking cursor the repair exists to prevent. The
  wait now drains the link until it goes quiet instead of guessing a duration.

- **A calibration measured on the Ultimate could not be replayed over a
  TeensyROM+.** A multi-socket file holds one table per socket, and choosing
  between them means knowing which socket answers `$D400`. A link with no SID
  config query cannot read that back — and "unknown" was being treated as the
  same answer as "an UltiSID core owns it, so no physical-chip table applies",
  so naming such a file in `[audio].dac_calibration_profile` reported it as
  holding no usable calibration and playback dropped to the 4-bit linear DAC.
  That is exactly the cross-backend reuse the option is documented for
  (measure over the Ultimate, replay over a cartridge in the same machine).
  Calibration runs now record which socket answered `$D400` before isolation
  began, and that record is believed on any link; a file predating it falls
  back to socket 1 — the default mapping — with a warning naming the assumption
  and how to retire it.

- **`--calibrate-dac` averaged glitched readings into the table, and later
  refused good runs because of them.** Individual capture slots occasionally read
  far off on one pass: across every refused capture kept for diagnosis, 1-6 codes
  out of ~86 glitched while the rest agreed to 0.004%. Those readings were folded
  into the affected code's level, and a wrong ladder entry is signal-correlated
  distortion — clean over a quiet passage, gross hiss once the material gets
  loud. Levels are now the median across passes, which discards the outlier
  outright, and the trust gate reads a 95th percentile instead of the worst
  single slot, so one transient no longer fails a ring that is otherwise perfect.
  Measured effect: a table measured over TeensyROM+ and one measured over the
  Ultimate, of the same 6581, went from agreeing at corr 0.844 / 18% RMS to
  **corr 1.0000 / 0.12%** — the figure two clean runs of one chip reproduce at.
  The link was never the variable.

- **A calibration profile named by a bare name could not refer to an existing
  file.** `--calibrate-dac` writes device-keyed names (`ultimate-<unique-id>`), but
  naming one of those in `[audio].dac_calibration_profile` resolved to
  `profile-ultimate-<unique-id>` and matched nothing — the obvious thing to type,
  since it is what is on disk. A name matching an existing file now wins; a name
  with no such file still gets the `profile-` prefix so new profiles file
  themselves as before.

- **A refused calibration capture is no longer discarded.** It is the only
  evidence for its own refusal, and repeating it costs a ~50 s hardware run that
  may not fault the same way. Refused captures are now written to
  `<data root>/calibration/unusable/`, with the codes, sample rate and
  diagnostics in the same file, and the failure names the path.

- **The test suite wrote a real calibration file into the developer's own data
  directory** on every run — `~/.local/share/c64cast/calibration/dac/` — because
  one test class persisted a calibration without redirecting `$C64CAST_DATA_DIR`.
  The isolation is now inherited rather than copied per class, so it cannot be
  omitted by a new one.

- **`--calibrate-dac` could write a wrong table from a capture whose levels
  moved.** Each pass of a capture drives the SID through identical codes, so a
  healthy rig's `pass_spread_frac` reads 0.01–0.2% — but the only gate was at
  10%, which exists to catch recording the room by mistake. A run reading
  0.6–2.5% therefore passed, and the table fitted to it agreed with the same
  chip's earlier table on 95 of 256 entries (correlation 0.565 — a worse mismatch
  than applying a *different chip's* table), after which `"auto"` preferred that
  file on every later run. Like any wrong ladder it is signal-correlated
  distortion: clean over a quiet passage, gross hiss once the material gets loud,
  so it presents as playback breaking partway in rather than as a bad
  calibration. Captures whose passes disagree broadly are now refused, with a
  message that says the input is right and asks what else is reaching that
  output; rings above 0.2% are marked marginal as they are measured. This is a
  check on the data, not on any particular link — a rig that reads in the healthy
  band is unaffected however it connects. **If a calibration of yours logged pass
  spreads near or above 1%, re-measure it** — or delete it and let `"auto"` fall
  back. (The gate this shipped with read the worst single slot, which refused
  good runs; see the slot-glitch entry under Fixed for what it reads now.)

- **A calibration measured over a link with no SID config API no longer applies
  itself silently.** Only the Ultimate can report which SID answers `$D400`, so
  every other link measures whatever is there and files it under one key. That is
  correct on a single-SID machine and a blend of two ladders on a machine with a
  second chip or with address mirroring on, and nothing on the host side can tell
  those apart — so the assumption is now logged where the table is chosen.

- **The static on the `$D418` DAC: a calibration table was being applied to the
  wrong SID.** `[audio].dac_curve = "auto"` chose the baked *emulated-UltiSID*
  table whenever the backend was an Ultimate, without checking which SID source
  actually answers `$D400` — the address the DAC writes to. On a board with a
  physical SID in a socket mapped there (the socket wins address mirroring, so
  the real chip is what you hear), that applied a ladder measured on different
  silicon: 19.4 dB worse than a table measured on the chip itself, and 17.6 dB
  worse than the plain 4-bit path. It is the constant buzz that made the 8-bit
  mode sound broken. `"auto"` now resolves the live `$D400` owner, and a
  populated socket with no calibration of its own falls back to the 4-bit path
  instead. **If you have a physical SID, run `--calibrate-dac` once** — a
  matched table beats the 4-bit path by 1.7 dB and runs 5.9 dB louder.

- **`--calibrate-dac` measured every socket after the first with unparked SID
  voices.** The 8-bit mode needs the three voices parked as DC sources, and that
  setup was written once at start-up, so it landed on whichever chip was mapped
  to `$D400` at the time. On a two-socket board the second socket's table was
  measured with no DC to scale. It is now re-installed after each routing change.

- **`--calibrate-dac` could measure a muted SID.** Routing a source to `$D400`
  does not make it audible — the Audio Mixer carries a separate per-source
  level. Calibration now routes the mixer to the source it is measuring and
  restores the previous levels afterwards. Previously this produced a capture at
  the noise floor, which looks like a broken capture device rather than a muted
  chip.

- **The audio underrun summary no longer contradicts itself.** It is emitted
  when the streamer stops, which happens once as a scene tears down and again at
  session teardown — so a run that reported real underruns was immediately
  followed by "clean session (no underruns)" from the second call, whose
  counters had just been cleared. The summary is now reported only for a run
  that actually fed the ring, and says "run" rather than "session", which is
  what it always counted.

- **Quick playback obeys every CLI flag again.** Playing media by positional
  argument (`c64cast clip.mp4`) built its config from a hand-picked handful of
  flags, so twelve of the twenty-one that the same command honours with
  `--config` were accepted and then silently ignored — among them
  `--frame-numbers`, `-D/--audio-device`, `--sample-rate`,
  `--dac-calibration-profile`, `--vision` and `--heartbeat`, plus the
  `C64CAST_DMA_PASSWORD` environment variable, which meant quick playback could
  not reach a password-protected Ultimate at all. Both front doors now share one
  merge, and a test asserts the whole flag map rather than the flags that
  happened to break.

- **TeensyROM: no more blinking cursor, and the BASIC clear loop actually
  runs.** LaunchFile left the clear-loop program at `$0801` with its link
  pointer zeroed, so BASIC saw an empty program and dropped back to READY —
  where the editor's input-wait loop blinks the cursor and, because that loop
  rewrites `$00CC` on every pass, no write could switch the blink off. Bring-up
  now detects it and repairs it over DMA. The editor also stops eating the
  keystrokes the on-C64 keyboard control reads.

## [0.2.1] - 2026-08-05

### Added

- **The documentation is a website.**
  [kfox.github.io/c64cast](https://kfox.github.io/c64cast/) publishes all three
  books — User's Guide, Programmer's Reference Guide, Performance Card — plus
  the caveats, troubleshooting and extending notes, rendered from the same
  Markdown the PDFs are set from and republished on every push to `main`. Each
  book gets a contents page, a chapter sidebar, prev/next paging and a link to
  its typeset PDF; a section link resolves identically on github.com, in the
  PDF and on the site, because all three use GitHub's own anchor rule. The
  renderer (`scripts/build_site.py`) is stdlib-only and shares its reading of
  the Markdown — and every check that reading makes — with the PDF builder, so
  the two cannot disagree about what a page says. `make site` builds it
  locally; a pull request now proves every book still renders, which previously
  nothing did until release day.

- **Every book has a permanent download link.** A release now carries each PDF
  twice — `c64cast-users-guide-X.Y.Z.pdf` as before, and an unversioned
  `c64cast-users-guide.pdf` — so
  `https://github.com/kfox/c64cast/releases/latest/download/c64cast-users-guide.pdf`
  (and the same for `c64cast-reference-guide.pdf` and
  `c64cast-performance-card.pdf`) always serves the current release. The README
  links all three that way; every past release keeps its version-stamped copy.

- **`--doctor` names the opencv build that actually loaded.** Every opencv
  wheel — plain, contrib, and the headless variants — unpacks into the same
  `cv2/` directory under a different distribution name, so an installer will
  co-install several and the last one written wins. Installing the `vision`
  extra (or `all`) brings `opencv-contrib-python` along with mediapipe, which
  means the `opencv-python` version c64cast pins is not the one that runs, and
  nothing said so. ENVIRONMENT now reports the build in place, flags it when
  more than one distribution is providing `cv2`, and warns when the winner is a
  headless wheel — the cause of `[preview]` opening no window.

### Fixed

- **DAC audio can no longer come up silent for a whole session.** On some
  machines a run would play no audio at all from the first frame to the last —
  never a dropout, never a recovery, and the video also ran noticeably fast.
  Three writes start the NMI audio consumer, and the write transport is built to
  absorb a dropped write rather than fail loudly; if any of the three went
  missing the consumer never started, and nothing on the host noticed (the fast
  playback was the pacing loop correctly chasing a reader that never read). The
  bring-up now checks that the consumer actually started and re-sends the writes
  if it didn't, up to five times, logging when a retry was needed and warning
  outright if it never takes. A consumer that dies mid-session also warns now
  instead of playing out as unexplained silence. `--calibrate-dac` uses the same
  verified bring-up, so a run can no longer spend 50 seconds measuring nothing.

- **Ensemble systems no longer record over each other.** `[recording].path`
  cascaded from the master like the rest of the section, so every system in a
  wall opened a `cv2.VideoWriter` on one file and finished with a single
  truncated recording — silently, since the writers have no way to detect the
  collision. `path` is now per-system like `ultimate64.url`: leave it unset and
  each system writes `recording-<system>.mp4`; set it and that path is used as
  written. `enabled` still cascades, so recording a whole wall is still one
  key. `--doctor` reports an error if two systems are pointed at one file
  explicitly.

- **`--doctor` findings can no longer go missing.** The report printed only
  those categories named in a hard-coded list, so a check reporting under any
  other name returned findings that never reached the screen — indistinguishable
  from passing. Unlisted categories now print after the known ones.

- Corrected the troubleshooting advice for a shadowed opencv, which prescribed
  reinstalling `c64cast[all]` — the install that causes the shadowing.

### Changed

- **The README is a landing page again.** It had grown a reference section for
  each surface it introduced — the full keyboard table with its chord
  precedence rules, nine config-discovery commands, the machine-settings
  precedence, the SID-player rationale — all of which the books now state
  properly, from the code. Those are cut down to what someone deciding whether
  to install this needs, and the space goes to what was missing: the pixel
  effect chain (four of the eight effects were listed, and the chain not at
  all), the live performance surfaces (a clip grid, a beat grid, pad LEDs,
  looks and the `/perf` console had no mention outside the docs list), audio
  files as quick-playback arguments, the bitmap spectrum overlay, and the
  character ROM your first run reads off your own machine.

## [0.2.0] - 2026-08-04

### Added

- **c64cast reads the C64 character ROM off your own machine.** Every glyph
  drawn as C64 text — the text overlays on bitmap modes (`scrolling_text`,
  `marquee`, `corner_text`, `logo`), `big_text`, the on-C64 menu, the
  oscilloscope's labels, the preview window and the stream recorder — comes from
  the character ROM. Previously the only way to have one was to find a dump and
  drop it at a working-directory-relative path in a source checkout, which meant
  an installed c64cast could never resolve it and a user report of "the
  scrolling text looks bad" was, in full, "there is no character ROM". Now the
  first run against a machine reads it off the C64 and caches it at
  `<data dir>/roms/chargen.bin`; every later run picks it up. It costs about a
  second, once per machine, and no ROM bytes are shipped or downloaded — they
  move from your hardware to your disk. `--dump-char-rom` re-reads on demand
  (after swapping in a different character ROM, say), `--install-char-rom PATH`
  installs a 2 KB or 4 KB dump you already have with no hardware involved, and
  `[hardware].dump_char_rom = false` turns the automatic read off. `--doctor`
  reports which ROM is in use and whether it verifies.
- **A second book: the Programmer's Reference Guide** (`docs/reference/`), the
  volume you open at the page you need rather than read in order. Seven
  chapters: the configuration language and its precedence rules, the catalog
  of every scene and overlay, the display pipeline from frame to VIC-II
  register, the sound path in both directions, the link into the Commodore's
  memory and what lands there, every input and output that reaches the show from
  outside, and how to extend the program itself. Its appendices
  are *generated* from the code by `scripts/gen_reference_appendices.py`: every
  configuration section and field, every scene key, every overlay parameter, the
  overlay against display-mode matrix, every generator and effect, every
  live-tune target, every command-line flag, every packaged example and every
  optional install extra. They
  read the same definitions that answer `--describe`, `--compat` and
  `--print-schema`, so a table in the book cannot disagree with the program.
  `make reference` renders it, `make books` renders every book, and `make
  reference-appendices` rewrites the generated ones — which CI checks for drift.
- **A third book: the Performance Card** (`docs/card/`), two printable pages for
  the desk beside the controller. Every control surface and what it is mapped to
  out of the box, the pad chords and pad-light states, every live-tune target,
  the clip-grid and tempo syntax, the console's routes, how a channel addresses
  one Commodore of an ensemble, and the four commands worth running before the
  doors open. `make card` renders it; its live-target table is generated
  alongside the reference guide's appendices. It takes the `card` layout: the
  same palette, faces and tables as the other two books, set two-up at 8.5pt
  with no cover, contents or chapter openers.
- The GitHub release now carries **every book**, each stamped with the version:
  the User's Guide, the Programmer's Reference Guide and the Performance Card.

### Removed

- **`docs/usage.md` is gone.** Its 1,867 lines were the end-user reference
  before there was a book to put them in; every part of it that was not already
  duplicated by the User's Guide has been rewritten into the Programmer's
  Reference Guide, which states the same rules from the code rather than from
  prose that had drifted from it. Every link that pointed there now points at
  the chapter or appendix that answers the question, and a test fails if a new
  one appears.

### Changed

- **The Programmer's Reference Guide now documents what a reload actually
  re-reads, and the signals.** `POST /reload` was described as re-reading the
  configuration and rebuilding the playlist, which overpromised: a reload swaps
  `[[scenes]]` and `[interstitial]` and nothing else — the connection, the audio
  path, the capture device and even `[playlist]`'s own `loop` and
  `fade_duration_s` are fixed at startup. Chapter 6 now says so, and gains a
  *Signals* section covering `SIGHUP` (the control-plane-free spelling of the
  same reload, POSIX-only, which the User's Guide advertised and the reference
  never mentioned), `SIGTERM`, and the ensemble rule that each system re-reads
  its own file while the master is not re-read. No behavior changed.
- The `hopalong` generator's live target `source.a` is now **`source.shape`**.
  Every other live target is named for what turning it does — `drift_speed`,
  `ring_freq`, `zoom_speed` — and this one was named for the letter Barry
  Martin's map gives the constant, which tells a performer looking at a knob
  label nothing. Sweeping it reshapes the attractor, so it is `shape`. The
  constant is still `a` in the implementation, where it matches the published
  map. `source.a` was never settable from a config; the one thing this breaks is
  a hand-written `[[midi.mappings]]` entry naming it, which now silently fails to
  match — rename the target.
- `[preview] charset_path` now defaults to unset, meaning "use the character ROM
  c64cast resolved". Set it to force a specific file. A configured path that
  doesn't exist now warns and falls back to the built-in font instead of raising
  `FileNotFoundError` and killing the run.
- The built-in fallback font now fills screen codes `$80-$FF` as the reverse-video
  complement of `$00-$7F`, like the real ROM. They were blank, so with no
  character ROM installed `big_text`'s glyph pixels, the `blocks` PETSCII style
  and most of the PETSCII shading ramp — all of which paint `$A0` and up —
  rendered as nothing.
- The User's Guide build now renders *a book* rather than *the guide*, in
  preparation for a second volume. `scripts/build_guide.py` is
  `scripts/build_book.py --book-dir docs/<book>`, the Typst template and the
  vendored OFL fonts moved from `docs/guide/` to `docs/shared/`, and each book's
  `book.toml` names the layout it takes. `make guide` and the released PDF are
  unchanged.
- **The reference guide's generated appendices are set as two columns instead of
  four.** A field's name, type and default are three facts about one setting,
  and given a column each on a 6.24in page they left the description — the only
  part written for a human — about a third of the measure and four words to a
  line, with a single field running most of a page. They are now stacked into
  one fixed-width column with the description taking the rest, at the same width
  in every such table, so a scene key, an overlay parameter and a CLI flag all
  line up down the book. The reference is 20 pages shorter for it.
- **Chapter and appendix cross-references are links.** "See Appendix F" in the
  prose jumps to Appendix F, and every line of the table of contents jumps to
  its page. A reference to a chapter the book does not have now fails the build,
  which is what catches a renumbering the prose was not told about.
- **Every section of every book can be linked at, and the chapter opener pages
  are clickable.** The contents page already navigated; the opener page listed
  its sections and did nothing when you pressed one. Each `##` and `###` heading
  now carries an anchor, the opener bullets jump to the section they name, and
  the prose can link at a *section* — `[Fades](04-display-pipeline.md#fades)` —
  rather than only at a whole chapter, so a pointer can mean a row in a table
  instead of a page with a big numeral on it. The anchor is GitHub's own, because
  the Markdown is the book: the same link resolves on github.com and in the PDF.
  One that resolves nowhere fails the build and names the nearest ones it knows.
- **The Programmer's Reference Guide has an index**, and it is generated like
  its appendices. Every name the program can utter goes in — configuration
  sections and keys, command-line flags, scene types, overlays, display modes,
  generators, effects and live-tune targets — against the pages that discuss it.
  Locators are **clickable page numbers** in the PDF and section links on
  github.com, from the one source, because the Markdown is the book in one place
  and there are no pages in the other. A key is listed bare, and again under its
  section where two sections share the name; a parameter belonging to a
  generator, an effect or a display mode is filed under its own name with the
  holder in parentheses, so `axis` is where you look and `axis (effect)` is what
  you find. A short curated set of ordinary words — "camera", "dithering",
  "display mode" — is in there for the reader who does not yet know what the
  program calls the thing. Section *titles* are deliberately not entries: a
  topic belongs to the contents page, and nobody looks up "Saving What a Run
  Changed".
- **Each appendix section opens with a worked TOML fragment.** A table of
  settings says what each one means and nothing about where the line is
  written, which left a reader who had found the right knob holding a name and
  no file. Every configuration section, scene type, overlay, generator, effect
  and live-tune mapping now shows the two or three lines that put it in a file,
  with a key's choices as a trailing comment. The fragments are generated from
  the same model as the tables under them and carry only real defaults — a key
  with no default is named in a comment rather than given an invented value.
- **The appendices are in alphabetical order.** Configuration sections and scene
  types were in declaration order, which reads well in the annotated example
  file and is no use in a book nobody reads in order — finding `[wled]` meant
  paging through nineteen sections in an order you could not predict. Overlays
  were already sorted.
- **A field's type and default say which is which.** The two lines under a name
  in every appendix table were bare — `str` over `'serial'` — and only obvious
  to somebody who already knew. They are now labeled *Type:* and *Default:*.
- **The PDF navigates in the numbers it prints.** Page labels — what a reader's
  thumbnail strip and page-number box show — were lowercase roman from the cover
  to the index, on a book whose body is numbered in arabic, so "page 84" and
  page 84 were different pages. The switch at the start of the body now reaches
  the whole document, and a chapter opener is labeled instead of leaving a gap
  in the strip. Both books.
- **Reference tables read better.** No table cell justifies any more: Appendix
  F's "Declared by" lists fourteen generator names down a 1.6in column, and
  justified they came out as two words a line with a river through them. Ranges
  and value counts are set as literals rather than in the body face, where their
  digits stood taller than the mono names beside them and read as the largest
  thing in the table. Appendix E no longer repeats `source.` and `effect.` on
  every one of fifty lines — the holder is stated once above each table — and
  index entries are no longer emboldened, which was setting one column in two
  faces at two apparent sizes.
- **Four more tables say a repeated name once.** Appendix D's rule table gave
  every refused overlay a row, and ten of the thirteen rows read "needs a
  text-capable mode (petscii/blank/hires/mhires)" — the same sentence read ten
  times to learn one thing. It is by the rule now, four rows for the three
  rules, and the appendix fits the page its matrix is on. Appendix B printed
  `duration_s`'s sixty-word description under nine of its ten scene types; it
  sits with the keys every scene takes, over a line naming `video` as the
  exception. Appendix F and the Performance Card drop the holder from every
  live-target row the way Appendix E did — Appendix F heads each section with
  the holder itself (`mode`, `effect`, `source`, `scene`) and says what it
  holds, the card puts it in the column heading.
- **The books, and the code, are spelled in American English.** `colour`,
  `behaviour`, `quantise`, `analyser`, `centre`, `catalogue`, `licence` and the
  rest. The program has always named itself in American English — `color_match`,
  `grayscale`, `palette_mode` — so the prose was disagreeing with the keys it
  was telling the reader to type, sometimes in the same sentence. The `grey` /
  `gray` color alias is untouched: both still resolve.
- **The books no longer talk about their own build.** "Generated from the code by
  `scripts/gen_reference_appendices.py`. Edits here are overwritten" opened every
  appendix and the index; the glossary explained that it was hand-written
  "because a machine has no opinion about which words a reader will not know".
  None of that is for the reader. What the appendices *are* is still said once,
  in the introduction and the colophon, where it belongs.
- **No listing wraps.** Typst wraps an over-long line in a code block rather
  than complaining, and a wrapped listing does not look broken — it looks like a
  line the program never printed. `--profile`'s sample came out as six lines of
  four and a class definition wrapped mid-signature. Every listing in all three
  books now fits its measure, and a test holds them to it.
- **The Programmer's Reference Guide is illustrated.** Five diagrams, for the
  five things in it that are spatial and were being carried entirely by prose:
  the precedence ladder with the extra rung an ensemble inserts, the twelve-step
  display pipeline with the setting that enters at each step, one hardware cell
  in each of the four picture modes with the bytes that color it, the DAC path
  against the sampler path with what each costs the 6510, and the 64 KB during a
  bitmap scene — the VIC's banks drawn as what they are, four 16 KB windows on
  one memory, with color RAM outside all of them. They are drawn by
  `scripts/make_reference_diagrams.py` in the books' own faces and palette, and
  committed; `make reference-figures` redraws them.
- **The books' symbols no longer depend on the machine that built them.** Jost
  has no ✓ and no →, and Typst was filling them from whatever was installed — so
  the compatibility matrix was set in a heavy upright check locally and a thin
  slanted one in CI, from the same source file. Both marks are now drawn by the
  template, Typst's own fallback is off, and a new test fails on any character
  the two vendored faces cannot draw. (One had already got through: a `⇒` in a
  generator's docstring, which is in neither face and was printing as a gap.)
- Inline code in the books is set at 1.08em rather than 1em. The two faces agree
  on x-height but Inconsolata's ascenders and capitals run 12–17% short of
  Jost's, which is what the eye compares when the two meet inside a line, so
  every `[section]`, `--flag` and `6581` sat visibly low in its sentence.
- The appendices' opener pages no longer print the backticks around a section
  name — the section list was being quoted as a string rather than converted —
  and Appendix B's scene types are headed by the type's name rather than by
  `type = "webcam"` repeated ten times.
- **The Performance Card's pad-light table had its columns labeled backwards.**
  `Pad | State` sat over rows reading `Bright | Playing`, which is a light and
  what it means, not a pad and its state — and the reverse of the same table in
  the reference guide. It is now `Light | Means`. The card's gesture table also
  lost pinch-to-resume when its "paused" column was replaced by the performance
  column; the row carries it again.
- **The reference guide's keyboard table lists <kbd>SPACE</kbd>**, which the card
  already had, so the two are the same table. Key names are keycap chips in all
  three books instead of chips in two of them and bold text in the third, and
  they are uppercase throughout, as the keys are.
- The reference guide's glossary defines **C64U**, **TeensyROM+** and **Extra**,
  and its introduction stops promising that the book carries no reasoning: it
  prints the measurement behind a default where there is one, and leaves *which
  other approaches were tried* to `docs/architecture.md`. One passage that was
  pure history with no decision attached is gone.
- The reference guide warns where it should: a `launcher` scene hands the machine
  away and can stop answering the modifier keys, and `--calibrate-dac` replaces
  an existing table with no prompt and no backup.
- Appendices G and H are reachable from the prose — the flag list from the
  notation section, the example index from the section on `example:` names.
  Nothing referred to either of them before.
- The `fireworks` generator's description no longer carries an internal note
  reference, which was published verbatim in Appendix E and in the release PDF.
- **The reference guide can get you connected.** Chapter 1 gains "Naming the
  Hardware": every connection-target scheme, what a target decomposes into in
  `[hardware]`, `[ultimate64]` and `[teensyrom]`, the `dma_port` / `tcp_port` /
  `baud` / `storage` query parameters, what `-s NTSC` / `-s PAL` actually
  changes and why it belongs in machine settings, and a table of the three C64U
  network services against what stops working without each. The one string that
  picks both the backend and its endpoint had appeared only inside two example
  commands, and the volume you open when a machine will not answer never said
  which switch to throw.
- Quick playback's extension-to-scene mapping is in the reference guide's prose
  rather than only inside a help string: which argument becomes which scene,
  what a directory or a glob does, and how a URL's timestamp becomes `start_s`.
- **`[interstitial]` is documented** (Chapter 2) — what the card is, the styles
  it takes, the three things that bypass it, and what else happens at a scene
  boundary.
- Every scene type in the reference guide's catalog now opens with the same
  three facts: which extra it needs, where it looks for files when `file` is
  omitted, and which display modes it accepts.
- **The live-tune write-back is documented** (Chapter 6): what `--overwrite`
  does, what is offered back at exit and what is not, and a warning that saving
  rewrites the whole configuration file from the settings in memory and keeps
  one `.bak` deep.
- **The reference guide's two vaguest chapter titles now say what is in them.**
  "Inside the Machine" is *The Link and the Memory Map*, and "Everything
  Outside" is *Inputs and Outputs* — which is what a reader scanning the
  contents for MIDI, WLED or recording can actually find. The chapter numbers
  are unchanged, so every cross-reference still lands where it did.
- **Extending c64cast is its own chapter** (7) rather than the tail of the
  memory-map chapter. Writing a scene, an overlay, a generator or an effect is
  contributor material, and it was sitting inside a user-facing chapter after a
  write budget. It is appended rather than inserted, so chapters 1 to 6 keep
  their numbers.
- ASID and the MIDI scene are no longer written out twice. Chapter 2's
  catalog entries state what a *configuration* needs — the keys, the extra,
  the ports — and defer the mechanism to Chapter 4, which is the rule the
  introduction sets and was the one place the book broke it.
- **Every optional extra is listed in one place**, as the reference guide's new
  Appendix I: what each one unlocks, the module `--doctor` looks for, and the
  packages it installs — with the reason the install to ask for is
  `c64cast[all]` rather than one extra at a time. The chapters have always named
  an extra where a feature needs one; nothing collected them. The glossary moves
  to Appendix J.
- **The books keep the promise their notation section makes.** A setting that
  can move while a show is running now says so where it is defined: Appendices A
  and B mark a field *live-tunable* and name the target a knob reaches it by
  (`[color].dither` is `mode.dither_method`, which is exactly the pairing a
  reader could not guess), and mark it *menu-live* when the on-C64 menu carries
  it as a knob. Appendix E writes each generator's and effect's parameters the
  way a `cc_map` has to spell them — `source.speed`, not `speed` — so a line can
  be copied straight into a mapping.
- **The performance card's live-target list says who declares each target.** A
  knob mapped to `source.ring_freq` does nothing unless `moire2` is the
  generator on screen, and the column that says so was the one the card dropped
  for space. It is back, and it names them: the modes, effects and generators
  that declare a target, spelled out, because the question at the console is
  whether the thing on screen is in that list and a count — `14 generators` —
  cannot answer it. A target nearly everything declares is written as its
  exceptions instead (`all but fire, mandelbrot, …`). Still two pages.

### Fixed

- **The reference guide offered `https://` as a way to reach a C64U.** The
  machine's REST service is plain HTTP on the ordinary port and has no access
  control of its own, so a reader who followed the table got a connection
  failure. The connection-target tables now show `http://` only, and the prose
  says what the link actually is and points at `SECURITY.md`.
- `--doctor` never reported the `wled` extra, so a missing `zeroconf` — the one
  thing standing between `[wled].listen` and a WLED app that can discover the
  virtual device — showed up as silence in the one command whose job is to say
  what is missing. All twelve extras are now probed, and a test holds the list
  to the extras the package actually declares.

## [0.1.0] - 2026-07-30

The first public release. c64cast has been in daily use against real hardware
since June 2026 — this is the point where it becomes installable rather than
cloneable.

### Added

**Two hardware backends, selected by URI.** `-u u64://HOST` (or `http(s)://`)
drives an [Ultimate 64](https://ultimate64.com/), Ultimate II+, or Commodore 64
Ultimate: memory writes go over the Ultimate DMA Service on TCP port 64 with
REST for the handful of operations that have no DMA equivalent. `-u tr://`
drives a [TeensyROM+](https://lectronz.com/products/teensyrom) cartridge in an
original C64 over auto-detected USB serial, an explicit serial device
(`tr:///dev/cu.usbmodemXYZ`, `tr://COM3`), or raw TCP (`tr://HOST[:PORT]`).
Per-link knobs ride along as query parameters (`u64://host?dma_port=64`,
`tr:///dev/…?baud=2000000`). `$C64CAST_URL` is the environment fallback.

**Ten scene types**, mixed freely in a TOML playlist with per-scene durations
and an "UP NEXT" interstitial between them:

- **video** — MP4/MKV/etc. with its soundtrack, paced off the audio clock so
  A/V cannot drift. YouTube and other streaming URLs resolve through yt-dlp.
- **webcam** — live capture quantized to any display mode in real time.
- **slideshow** — still images from a directory or glob, aspect-fit.
- **waveform** — plays a `.sid` on the real chip through a small player PRG
  DMA'd into C64 RAM (deliberately not the firmware's own runner, which
  hijacks the HDMI output), with a 3-voice oscilloscope driven by a host-side
  py65 SID emulator. Handles multi-SID tunes up to 8 chips on the U64's
  UltiSIDs, and matches 6581/8580 tunes to the installed chips.
- **midi** — bridge a live MIDI source into the real SID and scope each voice.
- **asid** — receive an ASID stream (DeepSID in a browser, SIDFactory II,
  Plogue chipsynth C64) and play it on the real SID with the same scope.
- **generative** — 20 procedural sources (plasma, tunnel, fire, mandelbrot,
  metaballs, game of life, fireworks, soap, …), optionally music-reactive.
- **launcher** — hand the machine over to a native `.prg`/`.crt` and reclaim it.
- **wled** — turn the C64 into a virtual LED matrix fed by a realtime pixel
  stream from LedFx or xLights (DDP or WLED realtime UDP).
- **blank** — a solid PETSCII canvas as a foundation for overlays.

**Six VIC-II display modes** — `petscii`, `mcm`, `hires`, `hires_edges`,
`mhires`, `blank` — each with its own vectorized quantizer (≈30 fps bitmap,
50/60 fps character modes over a LAN). The `[color]` pipeline shapes any source
before quantization: spatial dithering, perceptual color matching, per-cell
strategy selection, motion smoothing, scene fades, and a forced-palette mode
that remaps a frame onto a chosen subset of the 16 C64 colors — with a rolling
palette that re-clusters as the content changes. `--suggest-palette FILE`
analyzes an image or video and ranks the colors that represent it most
faithfully.

**Audio on the real SID.** By default, video and file audio play through the
U64's Ultimate Audio FPGA PCM sampler for high fidelity; the lo-fi `$D418` DAC
path (4-bit, or ≈6–7 bit via Mahoney companding) covers TeensyROM+, mic input,
and webcam audio everywhere. The sampler's effective clock ships calibrated, so
audio holds sync against host-paced video over long runs. `--calibrate-dac`
measures a per-machine DAC response curve through an HDMI capture device.

**Thirteen stackable overlays** — `scrolling_text`, `marquee`, `rss`,
`spectrum_petscii`, `spectrum_bitmap`, `clock`, `weather`, `callsign`,
`countdown`, `network`, `logo`, `big_text`, `obs_status` — composable onto any
compatible scene, with `--compat` printing the overlay × display-mode matrix.

**Ensemble mode.** One process drives N systems at once as a video wall, with
cross-system orchestration — a `big_text` message scrolling across every screen
as one continuous canvas, spans and mirrors — and audio-slot coordination so
the systems do not fight over the DAC.

**Live control surfaces.** The C64's own keyboard (C= pauses, CTRL skips, SHIFT
cycles the display style), an on-C64 menu for live scene tweaks, webcam hand
gestures, a FastAPI control plane (`/pause`, `/resume`, `/skip`, `/reload`),
MIDI CC mapped to any live parameter, and `SIGHUP` to reload the config. The
DJ/VJ layer adds a tempo/beat grid, a clip-launch grid with LED feedback on
grid controllers, a layerable chain of 8 pixel effects, snapshot-recall of
"looks", and a phone/web performance console.

**WLED bridge**, in three directions under one `[wled]` section: drive real LED
matrices *from* the C64's SID with no microphone, present c64cast *as* a virtual
WLED device that the WLED app and Home Assistant discover and control, and turn
the C64 *into* a matrix that LedFx/xLights stream pixels to.

**Quick playback.** `c64cast clip.mp4 tune.sid pics/ 'https://youtu.be/…'`
plays media straight from the command line with no config file, mapping each
argument to the right scene type by extension.

**Config authoring and discovery, all offline.** An annotated TOML reference,
an interactive wizard (`--init`), and a JSON Schema for editor autocomplete —
plus `--describe`, `--list-scenes`, `--list-overlays`, `--list-modes`,
`--compat`, and `--print-schema`, all generated from the same field metadata the
loader itself runs on, so they cannot drift from the code. `--doctor` collects
every config and environment problem in one pass. Machine-local defaults live in
`~/.config/c64cast/settings.toml` (written by `--save-settings`) and persisted
state in `~/.local/share/c64cast/`, both XDG-aware.

**Packaged demo configs.** Every feature has a runnable single-scene demo
shipped inside the wheel: `--config example:NAME` runs one, `--list-examples`
lists them all, `--print-example NAME` copies one out to edit.

**Preview and recording.** An optional local window mirroring what the C64 is
showing, and recording the same to MP4. Both are cv2-based, so neither needs an
optional dependency.

**Documentation.** A typeset User's Guide (10 chapters), a full config
reference, symptom-first troubleshooting, an extension guide, and per-module
architecture notes covering the hardware constraints and the dead ends behind
each design decision. Every install instruction and every missing-extra hint
names `uv`; `pipx` is documented once as an equivalent fallback.

The guide is attached to every release as a PDF, stamped on its cover with the
version it documents, so a downloaded copy can always be matched to the install
it describes. `make guide` renders the same thing from a checkout.

A config written by `--init` or `--save-settings` carries a `#:schema`
directive pinned to its own release, so an editor validates it against the
schema this version actually accepts rather than whatever is currently on
`main`.

**A leading `~` works in config-file paths.** `file`, `videos_dir`,
`songlengths_file`, `charset_path`, `model_path`, a `logo` overlay's file, the
recording path and `log_file` all expand `~/…` when they are used. A TOML file
has no shell to do it, and `glob`/`os.path` treat `~` as a literal directory
name, so such a path previously matched nothing. A `Config` still holds the
string as written, so serialized configs keep the `~` rather than baking in an
absolute home directory.

**Windows is a supported platform.** It always worked — casting to a real
Commodore from Windows is a routine path for one of the contributors — but the
published metadata and the User's Guide both called it untested, because CI only
ever ran on Linux. The test matrix now covers macOS, Linux and Windows across
Python 3.11–3.14, so the claim is backed on both halves: the matrix for the
host-side code, real hardware for the pipeline. The one platform difference worth
knowing is that `SIGHUP` config reload is POSIX-only; `POST /reload` on the
control plane does the same thing everywhere.

[Unreleased]: https://github.com/kfox/c64cast/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kfox/c64cast/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/kfox/c64cast/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kfox/c64cast/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kfox/c64cast/releases/tag/v0.1.0
