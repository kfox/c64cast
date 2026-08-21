# Changelog

All notable changes to c64cast are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and c64cast follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) over its *user*
surface — the CLI flags, the config schema, the `example:` names, and the data
directory layout. The Python API carries no stability promise while the version
is `0.x`.

Work lands under `## [Unreleased]`; cutting a release renames that section to
the version and stamps it with the date.

A version that needs the reader to *do* something — reinstall to pick up a new
extra, re-measure a calibration, rename a setting — opens with an
**Upgrade notes** subsection, above the Keep a Changelog ones. A version's
section is lifted verbatim into its GitHub release body, so that block is the
first thing anyone upgrading reads, and an instruction anywhere further down is
in practice not read at all. Releases that ask nothing of anyone leave it out.

## [Unreleased]

### Upgrade notes

- **The browser console needs the new `web` extra**, which did not exist in
  0.3.0. `uv tool upgrade c64cast` keeps the extras you installed *with*, and
  re-reads what each of them now contains — so a `c64cast[all]` install picks
  the console up on a plain upgrade with nothing to do. A narrower install does
  not: extras don't accumulate, so add `web` to the set and name every one you
  want in a single command.

  ```bash
  uv tool install --force 'c64cast[video,midi,web]'
  ```

  Without it, `--serve` reports the missing extra instead of starting. `c64cast
  --doctor` lists what the running install can import.

- **If your config's first line is a `#:schema` URL, replace it.** Earlier
  versions of the User's Guide told you to write one with a version number in
  it and edit that number when you upgraded — a maintenance task disguised as a
  one-time setup step. Your editor is otherwise still checking the file against
  whichever release you first installed: harmless at run time (nothing reads
  that line) and misleading while you edit, since it underlines settings that
  work and offers settings that don't. `c64cast --doctor` now reports it and
  prints the replacement, which you can also get on its own:

  ```bash
  c64cast --print-schema-path
  ```

  Put its answer on line 1 with `#:schema ` in front. That one names the schema
  inside your install, so every future upgrade updates it too and this is the
  last time you touch the line.

### Added

- **`[color].flicker_tolerance` — colors the C64 cannot draw, by alternating two
  of the ones it can.** The bitmap modes hold two screen pages over one shared
  bitmap and flip between them every video field, so the eye fuses each cell's
  pair into an
  intermediate shade — the trick Dragon Breed and Mayhem in Monsterland used. The
  alternation is driven by a C64-side raster IRQ and free-runs at the VIC field
  rate no matter how fast the host is pushing, so it needs no unusual link speed,
  no REU and no sampler; the host just uploads the pair, for one extra
  1000-byte page per frame. What it actually fixes is **gradient banding**, not
  the palette in general — spatial dither already synthesizes intermediate colors
  wherever there is texture to hide them in, so a chromatic gradient improves
  27-34% — how much depends on which pairs the machine's palette makes
  eligible — while a photograph improves ~1%.

  **In mhires it is worth much more, and on ordinary content.** Four colors
  across a 4-pixel-wide cell leave spatial dither far less room than hires' 8,
  so a photograph improves 4-32% rather than ~1%, and a chromatic gradient
  4-31%, depending on palette and setting. Two of a cell's four colors can
  blend: the pair the screen byte carries. Its third lives in color RAM at
  `$D800`, which is not VIC-banked and which both fields read from the one copy,
  and its background is a single register the swap writes once per frame — so
  both of those stay real hardware colors. Blending the background was measured
  and dropped rather than skipped: the frame's dominant color came out a real
  one on every fixture, so alternating `$D021` too would have bought a
  bit-identical picture. Needs `palette_mode = "percell"`; the global-4 modes
  choose one color set for the whole frame, so no cell has a decision for a pair
  to win, and arming says so.

  Widening the palette also forces the per-cell pick onto `error-min` (see
  `cell_strategy` below), which scores each frame's own reconstruction error
  rather than a temporally-smoothed histogram — and a pair's fused color sits
  deliberately close to a solid or another pair, so on video that pick
  routinely near-ties frame to frame. Fixed before this shipped: the pick now
  keeps the previous frame's trio unless a challenger's error is at least 25%
  lower, so a near-tie stops flip-flopping while a genuine color change still
  wins on a single frame. Unscaled by `motion_smoothing` — unlike the mode's
  other temporal smoothing, a genuinely-better trio's error improvement clears
  the margin on a single frame regardless, so there's no responsiveness cost
  to buy back by scaling it down.

  **Which pairs fuse was measured, not derived.** Nothing computed from the two
  colors predicts it: brightness distance correlates with scored verdicts at
  r=+0.26, chroma distance at +0.04, and a red-orange "warmth" axis fitted to an
  earlier session reached +0.32 before a blind re-score showed it excluding five
  of the eight steadiest pairs. So every pair the safety cap admits was scored
  by eye, blind, with shuffled positions and hidden solid controls, and
  `flicker_tolerance` is a cut across that table: `"off"` (default), `"clean"`
  (only pairs that fused — 24 colors on an Ultimate 64), `"subtle"` (30),
  `"visible"` (39, where the flicker is the point rather than a side effect).
  Pairs scored worse than `"visible"` are kept as a record but offered by no
  setting — measured, they reconstruct no better than `"visible"` does, so a
  setting for them would trade flicker for nothing. Pairs another `host_palette` brings under the
  cap that the sitting never judged are excluded rather than guessed at, and
  `scripts/diags/flicker_score_grid.py` is how the table grows — via
  `[color].flicker_score_pairs`, a diagnostic key that replaces the blend set
  with an explicit list, ignoring both the tiers and the luma cap. The tool that
  produces the table cannot be restricted by it, or a wrong tier would be
  permanent: a pair scored as flickering is in no blend table, so it could never
  be rendered to be re-judged. It cannot switch blending on by itself.

  **Off by default, deliberately.** A blended area alternates at 25 Hz (PAL) /
  30 Hz (NTSC), which is inside the recognized photosensitive-seizure band, so
  `[color].flicker_max_luma_delta` (default 0.075) limits
  how far apart in brightness a pair may be, which is the quantity that governs
  the hazard. It **warns rather than refuses** — above 0.10, and again above
  0.12 where modulation depth approaches the 20%-of-peak-white flash criterion —
  because a pair someone has looked at and accepted outranks a computed
  threshold; an earlier clamp at 0.12 withheld five of the eight cleanly-fusing
  pairs on the VIC-II rendering. Nothing unscored gets in however wide it is
  set. It is a safety control only, not a quality knob — see above for
  what decides whether a pair fuses — though it does bound what `flicker_tolerance`
  can reach: at the 0.075 default `"clean"` gets 5 of its 8 pairs on an
  Ultimate 64 and 3 of 8 on the VIC-II table. Which pairs
  qualify follows `[hardware].host_palette`, because what fuses is the light a
  particular machine emits. It also does not
  survive a 30 fps capture: a card records the flicker, not the fusion. c64cast's
  own preview and `[recording]` do show the fused result correctly, because they
  reconstruct from the write stream instead of filming the screen. See
  [caveats.md](docs/caveats.md) before enabling it.

- **The C64's screen, in the browser.** The console could author a show, start
  it, tune it and save it without ever showing you what any of that did —
  checking meant looking at the television the Commodore is plugged into. The
  Live screen now shows the picture, and it comes from the machine rather than
  from c64cast: the Ultimate 64's FPGA taps the VIC's own output and sends it
  as UDP, taking no C64 cycles and disturbing nothing a show is doing. So it is
  what the VIC actually painted, not what the render pipeline believes it
  wrote — and it is right for scenes c64cast does not draw at all, like a game
  under the launcher or a machine somebody is typing on.

  It runs only while you are watching. Press **Watch** to start it and **Stop**
  to end it; leaving the screen ends it too. That matters because the stream is
  a couple of megabytes a second, and the machine is also told to stop by
  itself if this host goes away without saying so. `[web].screen_fps` sets how
  often the host encodes a frame — not how fast the machine sends — and `0`
  turns the picture off entirely.

  Ultimate 64 only, and the console says so rather than showing a blank panel:
  an Ultimate II+ is a cartridge in someone else's C64 with no VIC of its own,
  and a TeensyROM+ has no video path at all. The zero-dependency `/perf` page
  gets the picture too — it is one `<img>`, with no script and no decoder.

- **Add a scene from the console.** Adding or removing a scene meant opening the
  *Source* editor, which made the most common change there is to a show file —
  "another clip like that one" — the one thing the generated form could not do.
  Each scene now has **Duplicate** and **Remove**, and there is an **Add scene**
  with a type picker under the list. A duplicate is a verbatim copy, name
  included, so a clip you have already tuned is one tap from a second. Removing
  the last scene is refused: a show needs one to play. These write immediately,
  so staged edits have to be saved or discarded first — inserting a scene
  renumbers the ones after it.

  A new scene has not named its media yet, which is the one thing a show needs
  before it will start. That saves, with the report saying what is still
  missing, rather than being refused — the first step of building a show cannot
  require the show to already run. Anything else that would stop it running is
  still refused with the file untouched, and a hand-written save in the *Source*
  editor is held to the old standard: it is a finished statement about the show.

- **Hand somebody a read-only link.** The console has had a viewer role since it
  had a token, and no way to give one out: sharing the screen meant sharing the
  credential that can stop the show. The Session screen now asks the host for a
  read-only link and shows it ready to copy. The token is minted on the first
  ask rather than at startup — a credential nobody asked for is one more thing
  to leak — and then kept, so the link still opens after a restart. It follows
  the show and can do nothing else: no start, no stop, no tuning, no config
  writes. Setting `[web].viewer_token` yourself still works and is used as-is.

- **A color field is the sixteen colors.** `border` and `background` accept a
  C64 color name *or* an index, and the form only ever offered the number —
  directly under help text saying you could write "light blue". A field that
  takes two kinds of value now offers both, with a selector saying which you are
  writing, and a color field draws the palette as swatches. The colors come
  from the host, so one that has matched the machine's own emitted palette shows
  the colors it really produces. `force_palette_colors` gains the same
  treatment: a count, or a whitelist picked from the swatches.

- **Keep what you tuned, from the browser.** A knob turned on a phone changed
  the show and then ended with it. A run started from the command line asks
  "save these?" as it exits; the host has no terminal to ask on, and a host that
  rewrote every show file it stopped would be unusable — so under `--serve` the
  changes were recorded and nothing ever acted on them.

  The Live screen's Tune panel now shows that record — every color-pipeline
  change since the show started, where it began and where it is now — and keeps
  it in the config the show is running from on one tap. The write is a patch of
  the file *on disk*, not a dump of the configuration the run was built from, so
  a field edited in the Settings view since the show started is still there
  afterwards; a save that would leave the config unable to load is refused with
  the file untouched and the changes still held, exactly as any other save from
  the console is. **Discard** drops the offer and touches nothing that is
  playing.

  A change no configuration field carries is listed and marked *runtime only*
  rather than silently dropped on the way to the file. A quick-playback run has
  no file to write to and gets the same pasteable block the command line prints.

- **A palette mode is kept too, in the scene it was tuned on.** `palette_mode`
  is the one live knob whose home is a `[[scenes]]` block rather than the shared
  `[color]` section, and for that reason nothing had ever written it back: every
  surface offered it, every save-back skipped it, and a palette dialled in
  during a show was gone at the end of it.

  It is now recorded with the scene that was on screen when you turned it, and
  written into that scene's own block — from the console's Save, from the exit
  prompt, and from `--overwrite` alike. Turn it during two different scenes and
  both are kept, separately: they are two settings, not one setting moved twice.
  A `[color]` knob swept across a scene change is still one change, as before.

  A palette turned on a scene the config never named — a launched clip, or a
  video the playlist inserted between scenes — has no block to be written into.
  Those are listed as *runtime only* rather than written into whichever scene
  happens to sit at that position.

- **A Tune panel on the console's Live screen** — the color pipeline, the
  generator and the scope, from a phone. A MIDI controller and the C64's own
  menu could always reach these 20-odd knobs; the browser reached the effect
  chain and nothing else, which made a phone a weaker controller than a MIDI
  box. Dither strength and method, palette mode, color matching, cell strategy,
  motion smoothing, auto-fit, every generator's speed and scale, the scope's
  gain: sliders for the numbers, pickers for the choices.

  The panel is generated from what the **running scene** actually declares, so
  every control on it writes somewhere — a blank scene has no generator and a
  PETSCII scene has no dither, and neither shows a slider that does nothing. And
  because the browser now turns a knob the same way a MIDI CC does, a
  color-pipeline change made from a phone is recorded like any other live tune
  — the same record a `c64cast --config …` run offers to write back into the
  config when it ends, and the console now makes that offer too (below).

- **Pause, resume, skip and jump-to-scene in the console.** The control plane
  has answered these since before the console existed, and the console offered
  none of them — so skipping a scene that was running long meant a keyboard at
  the machine or a `curl`. Transport now sits in the Live screen's tempo bar
  where a thumb already is, next to a scene list that says what is playing and
  jumps to any of it. A jump is a cut: it goes straight to the scene rather than
  playing the interstitial in front of it.

- **The `/perf` phone console reaches everything the host will take.** It is
  the zero-dependency page — the one a checkout that never built the browser
  console still serves, and the one to reach for when the bundle is not there
  on a gig day. It drew clips, the effect rack, the tempo and the looks, while
  pause, skip, jump, and every tune knob were things the host would accept and
  the page had no button for: skipping a scene running long meant a keyboard at
  the machine.

  It now carries the same panels the browser console does — **Tune** for the
  current scene's knobs, the record of turning them with a **Keep** that writes
  them into the running config, **Scenes** with a jump, and pause/skip beside
  the tap tempo. A run started from a terminal keeps its own exit-time offer,
  and says so if you tap Keep there.

- **The log follows you.** It lives in a collapsed bar on every screen showing
  the latest line, and opens in place. A save refused or a scene that failed
  mid-show is the host's own account of what happened, and it used to be a tab
  away from wherever you were when it landed.

- **The web console's Settings view now edits.** Every scene field and every
  setting in a configuration gets the control its type asks for — a switch, a
  picker holding exactly the values the loader accepts, a number, a box of JSON
  for a list or a table — with the same one-line explanation `--describe`
  prints beside it. Edited rows are marked and counted; **Save** writes them in
  one request, **Undo** drops one and **Discard** drops all of them. **Clear**
  is the other direction: it stops the file setting a field at all, and shows
  you what will apply instead before you commit to it. A save that would
  produce a file that cannot run is refused with the loader's own reason, the
  file untouched and your edits still on screen.

  The browser never writes TOML: `PATCH /api/configs/{ref}` takes named field
  edits and the host composes the file through the same dataclasses the loader
  reads, so a form save is a load-modify-dump of the tested serializer — and
  two consoles editing different settings do not overwrite each other's
  sections. What the form deliberately cannot do is structural: adding or
  removing a scene, changing a scene's `type`, and editing an overlay each
  rewrite a block rather than set a value in it, and stay with the *Source*
  editor.

- **A finder above the form.** "Only what this file changes" is the right
  default for reading a config and the wrong one for adding to it — the field
  you want is the one the file doesn't mention yet. Typing a name into *Find a
  setting* searches all 167 of them regardless of the filter, and a row you
  have edited is never hidden by either.

- **The Configs screen says when you are looking at the running show**, and
  offers **Reload scenes** there. Saving to disk and putting the change on the
  C64 are two different acts, and the reflex for the second one — restart the
  show — costs a machine reset.

- **The console says which of your changes a reload will actually apply.** A
  reload re-reads the file and rebuilds the scenes; `[audio]`, `[video]` and
  `[ultimate64]` were read once when the session started and their threads are
  already running. So the save now says so — *"Saved 3 changes. `[audio]` needs
  the session restarted; the rest apply on a reload"* — the staged-edit bar
  warns before you save rather than after, and when a reload would not be
  enough the running-show banner stops pretending it is and offers a **Restart
  on this config** beside it.

### Fixed

- **Double-buffered bitmap video no longer tears when the link is busy.** The
  bank swap is committed by a raster IRQ at line 248, which is inside vblank —
  but a host DMA write halts the C64's CPU for about a microsecond per byte, so
  an 8 KB bitmap push stalls it through roughly 128 raster lines. A swap IRQ
  that fired during one of those halts did not run until the halt ended, and by
  then the raster was deep into the visible picture: the top band kept showing
  the previous frame while the rest showed the new one. Measured over HDMI while
  sustaining ~231 KiB/s, 1.2% of frames were split this way, with the seam
  around a third of the way down.

  The handler now checks where the raster actually is before committing, and if
  it has been pushed past the safe window it leaves the frame staged and commits
  on a later field instead. A late frame is held one field longer rather than
  torn in half, and the frame rate is unchanged — capping write size also stops
  the tearing, but costs roughly 26 fps down to 15, so that is not what this
  does. Re-measured the same way afterwards: plain double-buffer split zero
  frames out of 1796, flicker blending 0.28%, at unchanged throughput.

- **The console's token no longer travels further than the terminal.** The host
  logs its login URL with the token in it, because that URL is the only way a
  phone gets in. Two destinations carried the same line and should not have:
  `--log-file` wrote it to a file that outlives the run and is not created
  `0600` — while the token's own store deliberately is — and the console's log
  buffer is served over the state feed to *every* client, a **read-only viewer
  included**. That second one was the worse of the two: a viewer link exists
  precisely so somebody can watch without being able to stop the show, and a
  token sitting in the log tail it receives handed it the ability to do exactly
  that. Since the host keeps its token across restarts, neither leak aged out.

  Both destinations now redact to `token=REDACTED`, keyed on the parameter's
  `token=` suffix so `viewer_token` and anything added later are covered too.
  The rest of the line survives, so the log still says which address was
  printed. The terminal is unchanged and still prints a URL you can open: it is
  the operator's own screen, and it is the one place the token has to work.

  If you have logs or bug reports from an earlier version, treat any token in
  them as public and restart the host to mint a fresh one.

- **Check says when a scene names media that isn't there.** A `video` scene
  pointing at a path that does not exist passed both **Check** and **Save**, and
  failed a few seconds into the run — after the link was open and the C64 had
  been reset. It is now reported in the same panel the loader's own diagnostics
  use, on a check and on a save. A warning rather than a refusal, because a file
  may legitimately arrive before showtime or belong to another machine in an
  ensemble; URLs and globs are left alone, the first because it is not a local
  path and the second because an empty glob is already an error.

- **`[ultimate64].url` takes the address you already know how to write.** The
  connection target `-u/--url` accepts — `u64://192.168.2.64` — went into a
  configuration file unchanged and then straight to an HTTP client that has no
  idea what that scheme is. The run failed with "could not reach the C64
  hardware", which points at the network, and the real reason sat at debug
  level. The field now reads `u64://HOST`, and the bare `192.168.2.64` the
  shipped example has always promised, as the `http://HOST` both of them mean.

  A `?query` knob is refused rather than applied, because in a file each of
  those is a field of its own and two ways to set `dma_port` in one document
  is a question about precedence nobody should have to ask; so is a `tr://`
  target, which names a backend the section it sits in has already named.

- **The hires cell picker can be saved.** `cell_pick` is offered as a live knob
  by every control surface — a MIDI controller, a WLED slider, the C64's own
  menu and now the browser — but nothing connected it to `[color]
  hires_cell_pick`, the setting it is the live face of. Turning it worked and
  the change was recorded; every save-back then quietly skipped it. It is now
  written like the rest of the color pipeline, and the mapping is held to the
  display modes' own registries by a test, so the next live knob cannot ship
  half-connected.

- **A validation error now says which file it came from.** A configuration is
  checked with your machine settings underneath it, so one stray value in
  `~/.config/c64cast/settings.toml` refused *every* configuration on the host —
  with an error naming a section, and nothing anywhere saying the value was not
  in the file on screen. The reflex is to hunt for a key in a file that does not
  contain it. Both editors now name the setting, its value and the file it came
  from, and only when all three are true: your machine supplies it, the file
  being edited is silent about it, and the failure mentions it by name.

- **Unsaved config edits survive leaving the screen.** Switching to Live to
  check something against the running show and coming back used to discard
  whatever was typed. Edits are now the console's rather than the screen's, and
  the Configs tab carries a dot while any are outstanding, so an unsaved change
  is visible from anywhere instead of only from the file it belongs to.

- **The console's finder falls back to descriptions.** Searching settings by
  name is right until you do not know the name — `cell_strategy` is not a word
  anybody guesses. A query that matches no name now searches what each setting
  *does*, and says that is what it did.

- **A saved configuration no longer absorbs your machine's settings.** Machine
  settings (`~/.config/c64cast/settings.toml`) are a layer *under* a config
  file: they say what this machine is, so a show file never has to. But every
  save-back measured "is this worth writing?" against the shipped defaults
  instead of against that layer — so saving a show config on the machine with
  the capture card wrote that machine's `[video] device` into the file, and the
  file then overrode the *next* machine's own setting. It applied to all three
  save-backs: the web console's form, the `--init` wizard, and the on-C64
  menu's live-tune save. They now measure against the machine layer, so a save
  writes what the *show* says and leaves what the *machine* says where it was
  set. Overriding a machine setting from a config still works and is still
  written — including overriding it back to the shipped default, which is a
  real answer and the only way to record it.

  Two consequences in the console: the Settings view marks a value that comes
  from your machine settings as unchanged (it shows the resolved value, but the
  file does not set it), and **Clear** on a field puts the machine's value back
  rather than the shipped default. A DMA password living in the machine
  settings, where it is legal, no longer blocks editing an unrelated config.

- **The console no longer scrolls sideways on a phone.** One long log line or
  one absolute path made the whole page wider than the screen — 1195 px of it
  in a 430 px viewport — because a panel grew to fit its widest content instead
  of letting that content scroll inside it. Every screen fits its viewport now.

- **Colors are now matched against the palette your machine actually emits.**
  The 16 C64 colors are fixed, but what they *are* depends on the machine: an
  Ultimate 64's video output and a real VIC-II's are about 25 counts per channel
  apart, and 60 apart on Orange. c64cast measured everything against the VIC-II
  rendering regardless, which is not a tint that the eye discounts — the
  quantizer picks colors by distance, so the wrong table sends pixels to the
  wrong color outright. On an Ultimate 64 that was **18.8% of pixels** and
  **+12.9% mean perceptual error**, worst on the grays, browns and orange.

  The new `[hardware].host_palette` defaults to `"auto"`, which asks the machine
  and needs no configuration: an Ultimate 64 reports its own palette, and
  anything else is driving a real C64 (an Ultimate II+ and a TeensyROM+ both do,
  and neither has a palette of its own) so the VIC-II rendering is assumed. Set
  it to `"u64"` or `"pepto"` to state it outright, or to the path of a VICE
  `.vpl` file to describe a machine with a custom palette loaded — an Ultimate
  won't serve its own `.vpl` over the network, so point this at a local copy.

### Changed

- **British spellings are gone from the prose, the code and the console.** The
  0.3.0 pass spelled the books in American English; everything written since had
  drifted back — `colour` in the web console's own field labels and swatch
  summary, `serialise`/`normalise`/`recognise` through the config store and the
  control plane, `behaviour`, `honours`, `artefact`, `judgement`, `catalogue`,
  `analyser`, `centre` across the architecture notes and the Reference. American
  English is now the rule for prose, code, comments, identifiers and commit
  messages alike, written down in CLAUDE.md and CONTRIBUTING.md so it stops
  drifting. `grey`/`gray` and `canceled`/`cancelled` are interchangeable and both
  stay; the `grey` color alias still resolves, as it always has.

- **Hires picks each cell's color by fitting the whole cell, not by sampling one
  pixel of it.** A hires cell gets two colors and one is the global background,
  so the remaining choice decides most of the frame — and it was being made by
  reading a single pixel per 8×8 cell. Fitting the cell instead cuts
  reconstruction error by about a quarter on photographic content (−24 % mean
  Lab, holding across every `dither` setting), and the gain scales with how much
  a cell's own pixels disagree: nothing on a smooth gradient, ≈−32 % on
  high-frequency detail. It also turns out to be *stabler* than what it
  replaced, which is the opposite of the trade the old approach was made for — a
  one-pixel read follows sensor noise directly, while a whole-cell fit averages
  it out, so a static subject under noise now stops rewriting the screen
  entirely instead of churning ≈33 bytes a frame. Costs ≈0.8 ms/frame, reusing
  the distance matrix the quantizer already builds. Set
  `[color].hires_cell_pick = "sample"` for the old behavior under a tight CPU
  budget.

- **A bad scene is now refused before the machine is opened.** Config validation
  checked each system's settings but stopped short of its scenes, so a mistake
  inside a `[[scenes]]` block — an unknown `type`, a `generative` `source` that
  doesn't exist, a `duration_s` on a video scene — only surfaced a few seconds
  into the run, after the link had been opened and the C64 reset. It is caught
  up front instead, with the same exit code (3) and the same message, plus the
  name of the scene that failed. Scenes marked `follower_only` are checked too:
  they are built when a broadcast picks them up, so a bad one used to surface
  mid-show. The web console gets this for free — **Check** and **Save** now
  refuse a config whose scenes won't build, rather than accepting it and
  failing at the next start.

- **The session lifecycle moved out of `cli.py` into a new `c64cast/app/session.py`.**
  Building each system's stack, running the playlists and tearing it all down
  were inlined in the CLI's `_run_session`, which meant a session could only
  exist for as long as the process did — there was no way to start, stop and
  restart one from a longer-lived host. They are now five composable steps
  (`validate_configs`, `build_session`, `start_services`, `run_foreground`,
  `teardown_session`) over a `Session` object, and `_run_session` is their
  composition plus the signal handling only a foreground CLI can do.

  Nothing about running c64cast changes. `cli` re-exports every moved name
  (`build_stack`, `teardown_stack`, `_run_playlists`, `StackBuildError`, …), so
  anything importing them from `c64cast.app.cli` — including the diag scripts
  under `scripts/` — keeps working. The one split worth knowing about is that
  config validation is now hardware-free and separable from the build, which is
  what lets a caller reject a bad config without disturbing a running session.

### Added

- **A configuration can be changed a setting at a time, without composing TOML.**
  `PATCH /api/configs/{path}` takes named changes — a section (or a scene index)
  and a field, with a value or `reset` to put it back to its default — and the
  host loads the file, applies them, writes it back through the config
  serializer and validates the result. This is what the console's generated
  *Settings* view will save through; today it is the API, and the *Source*
  editor is still how the console writes. A change that would produce a config that
  can't run is refused with the file untouched, and the text it replaced is kept
  as a hidden sibling either way. Two things it deliberately won't do: it can't
  add or remove scenes (that stays with the text editor), and it refuses a file
  carrying a DMA password outright, because writing that file back out would
  drop the password. Comments do not survive a save this way — the raw editor is
  still the right surface for a config you've annotated by hand.

- **The HTTP control plane can be locked with a shared token.** `[control].token`
  (or `$C64CAST_CONTROL_TOKEN`, which wins) is required on every route from then
  on — including the `/perf` console page and its WebSocket. Scripts send it as
  `Authorization: Bearer`, `X-C64Cast-Token` or `?token=`; a browser visits
  `/api/login?token=…` once and gets an `HttpOnly; SameSite=Strict` cookie, after
  which the console authenticates itself. An optional `[control].viewer_token`
  grants reads only: the console watches the show and displays a `read-only`
  chip, but pause, skip, reload and clip launches are refused.

  The default is empty, which is exactly today's behavior — open to anything
  that can reach the port. What changes without a token is one log line: binding
  a non-loopback `host` now warns that the run is drivable by anyone who can
  reach it. Being a shared secret over plain HTTP, the token is a lock on the
  door and not a reason to expose the port; `SECURITY.md` says where it stops.

- **A session supervisor (`c64cast/app/serve.py`), groundwork for the web
  console.** `SessionManager` owns one session at a time and moves it through
  `idle → starting → running → stopping → idle`, so a single process can start,
  stop and switch shows instead of ending when its show does. It carries the
  parts that only matter once a session outlives the command that started it:
  a settle window between teardown and the next start (the U64's DMA service
  refuses new connections for a few seconds after one closes, and a camera
  refuses to reopen straight after release), a poller that notices when a
  non-looping show ends by itself, a bounded log tail so a failure to start can
  be read somewhere other than the terminal, and a run marker that resets the
  machine on the next start if the previous run died mid-show.

  Nothing runs it yet — there is no new flag, config key or endpoint in this
  release, and running c64cast is unchanged. The daemon that drives it comes
  next.

- **`c64cast --serve` runs a web console host.** With the new `web` extra, the
  program stops being a one-shot command and becomes a server that owns the
  Commodore and starts and stops shows on request — the practical shape for a
  machine you would rather drive from a phone than from the terminal it is
  plugged into. Everything `[control]` already served (`/status`, `/reload`, the
  `/perf` console) rides the same port; between shows those routes answer `503`
  rather than pretending a session exists.

  The new routes are `GET /api/session` and `POST /api/session/{start,stop,
  switch,reload}` for the lifecycle, `GET /api/introspect` for the whole config
  model as JSON (including the `apply` and `applies_to` metadata the JSON Schema
  drops), and `WS /api/ws` for live state — the performance payload, the session
  state, and new log lines as they happen. The configuration is re-read from
  disk on every start, so editing a file and starting again runs the edit with
  no restart of the host. A start answers `202` and reports through the socket,
  because building a session takes seconds of hardware time; a start while
  something is running is a `409` rather than a silent replacement (that is what
  `switch` is for); and a config that will not run is a `422` refused before
  anything touches the machine.

  **This surface is never unauthenticated.** Unlike `[control]`, which stays
  open by default because that is what it has always done, a host with no token
  configured generates one, stores it `0600` under the data directory, and
  prints a ready-to-open login URL at startup. `[web].token` /
  `$C64CAST_WEB_TOKEN` / `token_file` choose your own, and `viewer_token` grants
  the same read-only role the control plane's does. New `[web]` section:
  `enabled` (the same switch as `--serve`), `host`, `port`, `autostart` and
  `settle_s`. The browser interface over this API is below.

- **The web console can browse and edit configs, and start the one you pick.**
  `[web].config_roots` lists the directories it may read and write `.toml` files
  in (empty = wherever the host was launched from), and `GET /api/configs`,
  `GET`/`PUT /api/configs/{path}` and `POST /api/configs/{path}/validate` are how
  a show gets authored without a shell. `POST /api/session/start` now takes an
  optional `{"config": "shows/gig.toml"}` naming any of them, so one host can run
  a whole folder of shows rather than only the file it was launched with.

  Files are named by root (`shows/gig.toml`) rather than by path, and nothing
  outside a root is readable or writable — including through a symbolic link
  planted inside one — nor is anything that is not a `.toml`. A save is loaded
  and validated before it lands, so text that cannot run is refused with `422`
  and the file is untouched; what was there is copied to a hidden sibling
  (`.gig.toml.bak`) first. A read returns the raw text *and* a per-field view
  marking everything still at its default, which is what the form editor will
  render. Ensemble masters read but have no such view — they are authored across
  several files — so they are edited as text.

  **A full token is now shell-equivalent on that host.** The root list bounds
  which files may be edited, not what a saved file can reach: a config names
  media paths and URLs a session will open. `viewer_token` cannot write at all,
  and `SECURITY.md` has the full note.

- **The web console has a console.** Opening a `--serve` host's address in a
  browser now gets a page rather than a route list: which configuration is
  loaded, what the machine is doing, the configurations the host can see,
  buttons to start, switch, reload and stop, and the host's log as it happens.
  State arrives over `WS /api/ws` instead of by polling, so the page follows a
  show started from anywhere else — another browser, a MIDI controller, `curl` —
  without being told.

  It ships **inside the package**, already built, so `uv sync` and `pip install`
  both give you a working console and neither needs Node. Node is required only
  to change the interface: the sources are Svelte 5 + Vite + TypeScript +
  Tailwind under `web/` in the repository, `make web` rebuilds them, and CI
  fails if the committed bundle and its sources disagree.

  The page is gated exactly like the API it talks to — nothing about it is
  public — so a browser arriving without the cookie is now given a form to paste
  the token into rather than a line of plain text. Scripted callers keep the
  plain-text `401` they had. The zero-dependency `/perf` performance console is
  unchanged and still on the same host, and a checkout that has never run
  `make web` falls back to it with a line in the log saying so.

- **The console can read and edit configurations.** A second screen lists the
  `.toml` files under `[web].config_roots` and opens one two ways. *Settings* is
  generated: every scene and every setting the file changes, each with the same
  explanation `--describe` prints, what it may be set to, and a `live` mark on
  the ones a running show picks up without a restart — and the values are what
  the loader actually resolved, so a machine setting or a default the file never
  mentions still shows through. Untick *only what this file changes* to see all
  167 of them. *Source* is the file itself, with **Check** to load it without
  saving and **Save** to write it back; a save that would not load is refused
  and the file is untouched, and what was there is kept in a hidden sibling.

  Editing is only as strong as the loader's own validation, which does not check
  scene `type` or a `generative` `source` against the registry — a typo there
  saves cleanly and fails when the show is built. `--doctor` still catches it.

  An unsaved edit survives clicking away to another file and is marked in the
  list, and a configuration has its own address (`/config/shows/gig.toml`) that
  a reload and the back button both respect.

- **The console has a performance screen.** *Live* is the beat grid, the clip
  grid, the effect rack and the look pads on one page: the tempo with a pulse on
  the current beat and **Tap** to set it by hand, a pad per
  `[[performance.clips]]` entry lit for what is playing and for what is waiting
  on its quantize boundary (with the count-in in beats), a bypass button and a
  slider for every knob the current scene's effects declare, and eight pads that
  recall a saved look — or store one, with **SAVE** armed.

  It drives the same engine a MIDI controller drives, so a pad tapped in a
  browser and a pad tapped on a grid are the same launch, and it is the same
  live feed the rest of the console already reads rather than a second
  connection. Pads are pressed and released rather than clicked, so a `gate`
  clip holds while your finger is down. An ensemble puts each machine on its own
  tab and its own address (`/live/left`). A read-only token watches all of it and
  drives none of it.

  The zero-dependency `/perf` console is unchanged and still the gig-day
  fallback; this is the same surface with the rest of the host beside it.

- **`scripts/diags/video_render_probe.py` now times the host as well as the
  link.** It reported the modeled cost of getting a frame *onto the wire* but
  nothing about the cost of producing one, so it could not answer whether a
  given machine is fast enough to drive c64cast at all — the question that
  decides whether the host can be a small single-board computer instead of a
  laptop. It now reports decode / render / total wall-clock milliseconds per
  frame alongside the existing per-region write cost, and names which side, if
  either, actually binds the source frame rate. `--threads N` pins decode and
  OpenCV to N threads so two machines can be compared by single-core speed
  rather than by core count, and the per-frame CSV gains `decode_ms` and
  `render_ms`.

  The measurement it makes easy to see: compose cost tracks the **source
  resolution**, not the display mode, because every mode resizes the source down
  to its own small target and that resize reads every source pixel. One frame
  costs ~30 ms from 4K in any mode and ~3.4-6.7 ms from 720p. So the media, not
  the renderer, is usually what decides whether the host or the link is the
  bottleneck — which is why the verdict line names pre-scaling as the fix.

### Fixed

- **PAL SID tunes no longer play ~20% fast.** The C64-side SID player chains
  PLAY onto the kernal's CIA #1 Timer A interrupt, which the KERNAL runs at
  ≈60 Hz on *both* standards — it is a wall-clock service (TI$, SCNKEY, cursor
  blink), not a frame interrupt. Nothing in the `.sid` path ever reprogrammed
  it, so a tune composed for PAL's 50.12 Hz ran at 60.0 on a PAL machine as
  much as an NTSC one: **+19.7% tempo**, across roughly 80% of a full HVSC.
  New `[ultimate64].sid_play_rate` (default `"auto"`) sets the latch to the
  tune's own frame rate. `"off"` restores the previous behavior for anyone who
  knows these tunes at NTSC speed and prefers them that way, and an explicit
  number in Hz pins every vsync tune to one rate. CIA-timed (multispeed) tunes
  self-time from their own INIT and are never overridden — the correction is
  gated on both the header's per-subtune speed flag and the timer value
  actually in place after INIT, so a tune whose header lies is still safe. The
  oscilloscope's host emulator now ticks at the real PLAY rate rather than
  assuming the video frame rate, which also fixes a latent scope/audio desync
  under `system = "PAL"`.
- **The kernal CIA #1 restore latch was the wrong standard's.** Both the ASID
  ring player and the REU audio pump wrote `$4025` back at teardown while
  documenting it as the NTSC default; `$4025` is PAL's and NTSC's is `$4295`.
  The jiffy clock therefore ran ~3.8% fast on NTSC after either teardown, until
  the next reset. Both now go through `c64.kernal_cia1_latch(system)`.

### Added

- **`[ultimate64].system` defaults to `"auto"`** and is read from the
  Ultimate's live System Mode at startup. This one field feeds the CPU clock,
  the frame rate, the DAC NMI latches and the SID PLAY rate, and a hand-set
  value that disagreed with the machine moved all of them at once, silently.
  An explicit value still wins — it remains how you describe a machine the
  probe can't ask, such as a TeensyROM-driven C64 — but a disagreement now logs
  a warning and is an error-level `--doctor` finding. Falls back to NTSC under
  `--skip-probe` or on a backend without the setting.
- **`[ultimate64].sid_video_mode`** (default `"off"`) switches the Ultimate
  64's System Mode so the machine's PAL/NTSC timing matches
  `[ultimate64].system`, correcting SID *pitch* — the CPU clock differs 3.8%
  between the standards, about two thirds of a semitone. Independent of the
  tempo fix above and opt-in because it retunes the HDMI output (576p50 rather
  than 480p60), so every display and capture device has to re-lock. Applied
  live and volatile, followed by a C64 reset so the KERNAL re-runs its PAL/NTSC
  autodetect, and restored at teardown. Ultimate 64 only.
- **`[ultimate64].hdmi_scan_resolution`** (default `"auto"`) drives the
  Ultimate 64's HDMI upscaler. PAL timing at SD puts 576p50 on the wire and
  some capture devices cannot lock to it — the same machine at 720p50 captures
  cleanly. `"auto"` raises SD to HD only when `sid_video_mode` retimed the
  machine, so c64cast cleans up after its own change and leaves a machine it
  didn't retime alone; `"keep"` never touches it, and a scan-mode label pins it
  for the run. Newer U64 boards only (older firmware doesn't register the
  setting, and c64cast stays quiet when it's absent).

- **`--doctor` now reports unknown config keys as findings.** A key no section
  accepts is dropped as before, but it is now a warn-level row under a new
  `CONFIG` heading — named file, table, and key — and it counts in the summary
  tally. Previously it was a single `log.warning` printed *above* the report,
  where it reads as preamble next to the formatted rows: the run continued on
  defaults and the misplaced setting silently did nothing, so a broken config
  could look like a clean report. Normal (non-`--doctor`) runs still log the
  same warnings to stderr.
- **"Did you mean" now searches every config section.** When an unknown key is
  a valid key of some *other* table, the hint says so and names it — e.g.
  `palette_mode` under `[color]` reports that `[[scenes]]` accepts it and to
  move it there. That case is invisible to a within-section near-miss search,
  since the key is spelled correctly and simply lives elsewhere. Same-section
  typos still get the near-miss suggestion.

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

### Changed

- **`--version` now says which install it is.** It reports the directory the
  running code sits in after the number —
  `c64cast 0.3.0 (~/.local/share/uv/tools/c64cast/lib/python3.13/site-packages)`
  — because the number alone cannot answer the question people actually bring to
  it. `__version__` reads the *installed* distribution's metadata, so unpacking a
  release archive into a working directory moves nothing and the old number keeps
  being correct; the reader is then looking at a true statement about a different
  install than the one they changed. The path names the environment, and the tool
  that owns it: `uv/tools/`, `pipx/venvs/`, or a checkout.

- **The User's Guide says what an upgrade is.** "Keeping It Up To Date" was
  `uv tool upgrade c64cast` and nothing else — no way to check that it worked, no
  mention that extras don't accumulate, and no answer for the one mistake the
  three-line version invites, which is to treat c64cast as a folder of files and
  unpack a release over it. It is now an
  [Upgrading](https://github.com/kfox/c64cast/blob/main/docs/guide/04-setting-up.md#upgrading)
  section of its own, and the same symptom is answered in both troubleshooting
  appendices. A release's own notes now lead with how to install or upgrade, with
  the wheel and tarball labeled for the installers that fetch them.

- **The `#:schema` line no longer needs maintaining, and says so when it does.**
  Two new surfaces and one changed instruction. `c64cast --print-schema-path`
  prints the value for a config's first line — the schema *inside* the running
  install, worked out for where the config sits — and since an upgrade rewrites
  that file in place, the line stays true release after release. `c64cast
  --doctor` reports a directive that has stopped describing this install: pinned
  to another version, naming a path that no longer resolves, or naming a copy of
  the schema whose contents differ from the one this build generates (a leftover
  virtualenv from before a Python-version bump answers the path and describes a
  different program). It judges a path by content rather than location, so a
  vendored copy that matches raises nothing, and it never rewrites the file —
  a shared team schema and a deliberate pin are both legitimate, and neither is
  distinguishable from staleness by looking. And the User's Guide now tells you
  to ask for the path rather than to type a version-pinned URL and remember to
  edit it, which is the instruction that put stale pins in configs in the first
  place — the URL it printed as the example had itself said `v0.1.0` since 0.1.0.

  Pointing an editor at a *newer* schema than the program would be worse than
  pointing it at an older one, so nothing offers a moving "latest" address: it
  would stop flagging real mistakes and start suggesting keys the installed
  version rejects.

### Added

- **The web console's config browser is now a library, not a raw file
  listing.** The Session tab shows **Favorites** and **Recently launched**
  instead of every `.toml` under every root; the full, searchable list —
  sortable by name or by date, with a show/hide toggle for the packaged
  example configs — lives on the renamed **Editor** tab (`Configs` before).
  Any config can be starred from either tab. A file's name is now shown with
  its config root's path and `.toml` suffix stripped (subdirectories kept),
  double-clicking one starts it, and a persistent Start/Switch button in the
  tab bar tracks whatever config is currently selected, on every tab. Starting
  a show from any of these now switches to the Live tab once it actually comes
  up.
- **New and Duplicate buttons on the Editor**, alongside a Delete. Duplicate
  works on a packaged example too — the intended way to turn one into an
  editable starting point, since the examples root is otherwise read-only.
- **There is no more "host default" config.** Every surface used to treat "no
  config chosen" as a stand-in for whatever `--config` named at launch; the
  supervisor now reports that config's ref (`config_ref`) even before the
  first start, so the browser can preselect and show it like any other
  config instead of special-casing an empty selection.
- Favorites and recently-launched configs are **server-side state**
  (`~/.local/share/c64cast/console.json`), not one browser's `localStorage` —
  a phone and a laptop pointed at the same host see the same list. A launch
  from any surface (MIDI, a script, another console) counts as a recent, not
  only one started from this browser.

### Added

- **The web console's Live tab freezes a video in place instead of stopping the
  show.** Its old Pause button set the same machine-level `pause_event` the
  C64's own C= key does — a full halt, not what a performer reaching for
  Pause mid-set wants. A new **Freeze** button (and the rest of the Live DJ/VJ
  transport: a scrub bar, press-and-hold rewind/fast-forward, and A/B loop
  set/clear plus recall pads for a video's saved loop points) instead drives
  the same engine the MIDI transport surface has used since Phase 2 — pause
  in place with the audio muted, not a stop. It appears only for a scene that
  actually has a transport (a playing video), since a generator or a picture
  has nothing to scrub. The old pause/skip moved off the Live tab; they are
  still reachable from the legacy `/perf` page.
- **A visual color picker on the Live tab**, for a blank scene's border and
  background — the first `mode.*` live-tune target whose values are colors
  rather than a mode keyword, so it renders as the same palette swatches the
  config Editor already offers instead of a `<select>`.
- The Looks pads on the Live tab no longer look broken with a sparse set of
  saved slots: an empty pad now reads **+** rather than sitting disabled at
  30% opacity, and a tap on one saves the current look immediately (there is
  nothing there to lose) instead of requiring the SAVE toggle first.

### Added

- **A media picker for the Editor's `file =` fields**, so a video, `.sid`,
  image or program is chosen from a list of what is actually on disk instead
  of typed from memory. `GET /api/media?kind=&q=` browses
  `[web].media_read_write` and `media_read_only`, defaulting to the four
  directories the loader itself already defaults to (`assets/videos`,
  `assets/sids`, `assets/pictures`, `assets/programs`) — and offers the
  result as a combobox: free text, a glob, a comma-separated list and a
  directory (a per-play random pick, same as an unset `file =`) all stay
  typeable. Dropping a URL onto a scene sets its `file =` field directly.

### Added

- **Uploading media from the browser**, so a clip reaches the host without a
  shell. Drop a file onto a scene's card, or press the new **Upload…** button
  on any `file =` field: `PUT /api/media/{name}` streams it straight to disk
  (never buffered whole in memory — the host may be encoding video for a
  running show at the same moment) and PATCHes the field to wherever it
  landed. `[web].media_read_write` — a *kind → directory* table (`video` →
  `assets/videos`, and so on; empty means the four loader defaults, same as
  before) — replaces the unreleased `media_roots`, because which directory an
  upload of a given kind lands in has to be stated rather than guessed: a
  directory renamed `clips/` says nothing about what it holds. The kind comes
  off the file's own extension, so there is nothing to pick for a two-kind
  `generative` scene either. `[web].media_read_only` adds directories that
  are browsable but never a destination. Nothing already there is ever
  overwritten — a name already taken is renamed `clip-2.mp4`, `clip-3.mp4`,
  and so on — and a `viewer` token is refused the same way it is refused a
  config write. A `media_read_write` key that isn't one of the five known
  kinds (a typo like `vidoe`) now fails at startup instead of silently
  resolving to a directory no upload could ever reach.

### Fixed

- **SIGINT/SIGTERM now always end the process.** A stuck teardown thread used
  to leave a run that would not exit and would not log why: `daemon=False`
  playlist/supervisor threads are joined with a timeout and logged as
  abandoned, but the interpreter's own shutdown joins those same threads again
  on the way out — untimed, with no signal delivery, after `main()` had
  already returned its exit code. The installed entry point now force-exits
  once nothing can stall it (flushing output and `--log-file` first), so a run
  that cannot finish its own teardown releases the machine instead of hanging
  forever holding the DMA socket.
- A second SIGINT or SIGTERM now restores the default disposition for
  whichever signal actually arrived, rather than always SIGINT — so a repeated
  SIGTERM from a service manager is no longer caught forever, and a third
  signal genuinely kills the process. `--serve`'s host gained the same
  three-strike escape hatch the one-shot CLI already had.
- A paused scene's resume no longer sleeps through a stop signal: the
  post-reset wait now waits on the stop event instead of a bare `sleep(1)`, so
  a signal received during that second is honored immediately instead of after
  it elapses.

### Fixed

- **A refused start now says why, everywhere it can be tried.** A start or
  switch that failed its config validation used to answer `config did not
  validate (exit code 3); see the log` — the reason existed only in the log,
  because `session.SessionConfigError` carried an exit code and no message.
  It now carries the same diagnostic `validate_configs` already logs, so the
  422 names the actual scene or setting that was wrong. The shell's tab-bar
  Start button — reachable from every tab, and the most-used way to launch a
  show — used to swallow that failure into the browser's console instead of
  showing it anywhere; it now hands the refusal to the Session screen, which
  already owns a permanent problem line for it.
- **The web console pre-flights a config before ever claiming a start or
  switch**, rather than finding out from the 202 that followed it. `launch()`
  — the one function every launch surface (the tab-bar button, Session's own
  Start/Switch, a favorite's quick-launch, a double-click in the Editor) goes
  through — now checks the config first and refuses locally if it would not
  run. The check exposes `doctor.validate_load_result` (`--doctor
  --skip-probe`'s collect-all pass, never reachable over HTTP before) as a new
  `diagnostics` list on `POST /api/configs/{ref}/validate`'s report, so a bad
  config names everything wrong with it at once instead of one problem per
  click. That route also no longer silently validates an empty string when
  called with no body — an absent `text` key now checks the file as it
  stands on disk, which is what a pre-flight actually needs to ask about.
  The pre-flight's diagnostics list skips the installation-level checks
  (venv, hard deps, uv.lock, machine settings, data dirs, char ROM, extras)
  that `--doctor` still runs — those answer "is this machine set up right",
  not "is this config good to launch", and don't change from one Start click
  to the next. Live's own Start-the-host-default button now reports a
  refusal through the same `describeError` every other screen's problem
  line uses, instead of the bare exception text.

### Added

- **A `file =` field's picker searches the host instead of filtering the first
  500 entries a plain listing reached.** Typing into a media picker used to
  filter whatever `mediaOfKind` had already cached — one unfiltered listing per
  kind, capped at `MAX_FILES` — so an HVSC-sized tree or a large asset
  directory hid almost everything behind the ones the walk happened to reach
  first, and the `truncated` flag saying so was fetched and then dropped on
  the floor. The field itself is the search box: typing now debounces into a
  live `GET /api/media?kind=&q=` (the parameter has existed since uploads
  shipped; nothing called it with one before this), and a search past the cap
  is offered a "truncated" note instead of silently narrowing.
- **Reorder a show's scenes from the web console**, without opening the text
  editor. `add_scene` and `remove_scene` had a route each; the order of a show
  was still a text-editor job, which was the one structural change that never
  got one. **↑**/**↓** chips on each scene block move it earlier or later
  (`PATCH /api/configs/{ref}/scenes/{index}`, body `{"to": n}`), reusing the
  same `_rewrite` spine as every other structural edit — the `.bak` sibling, the
  ensemble and secret refusals, `partial=True` so reordering a half-built show
  isn't refused for a scene that names no media yet. Disabled while an edit is
  staged, the same as *Duplicate* and *Remove*, since renumbering the staged
  edits to match a reorder is exactly the reconciliation those two already
  refuse rather than attempt.
- **A real progress bar for media uploads, with a Cancel button.** Dropping a
  large clip onto a scene, or picking one with the **Upload…** button, used to
  show `Uploading clip.mp4…` and nothing else until it finished or failed — no
  percentage, no way to stop it. `uploadMedia` now goes over
  `XMLHttpRequest` instead of `fetch` (the only browser API that reports
  request-body progress), so the console's first `<progress>` fills in as the
  bytes actually land, going indeterminate if the browser can't measure a
  total. Canceling aborts the request through an `AbortController`; nothing
  changes on the server side — the aborted read already drives the same
  cleanup path a network failure does, and the partial file is unlinked either
  way.

### Added

- **The Live screen can now be driven from the keyboard.** Space
  pauses/resumes, `t` taps the tempo, `n` skips to the next scene, `f`
  freezes/unfreezes the video, `l` toggles the A/B loop, `[`/`]` rewind/fast-
  forward while held, `1`–`8` launch a clip slot, and `?` shows or hides the
  list on screen. Every shortcut backs off the moment a text field, a select
  or a button has the focus — so tabbing to a button and pressing Space still
  activates that button rather than pausing the show — and none of them
  reaches past a read-only console.

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

- **Less data on the wire every frame, and no more stalls on scene cuts.** The
  delta-upload path decided how to split a changed region into writes by
  counting bytes, which is the wrong currency on socket DMA: a write there
  costs ~5.2 ms regardless of payload up to ~2.4 KB, so splitting a region into
  pieces multiplied its cost while the byte count obediently went down. It now
  prices both options against a per-backend measured cost model and picks the
  cheaper, and a change that covers most of a region pushes only the range that
  actually changed rather than re-uploading the whole thing.

  Measured on an Ultimate 64 over a minute of video per mode, at an unchanged
  write rate and frame rate: **~25% fewer bytes** on the host-DMA bitmap path
  (220 → 166 KiB/s) and **~28% fewer** in the character modes (41 → 30 KiB/s).
  Offline against the same clip, the frames that used to be split worst — wide
  sparse changes, i.e. scene cuts — went from 104 ms to 26 ms, and the mean
  frame from 18.1 ms to 13.3 ms.

  Nothing to configure. The bitmap figures are for the host-DMA path; an
  Ultimate with its REU enabled already stages `hires`/`mhires` bitmaps through
  the REU bank-swap instead, and those are unaffected (their screen and color
  RAM still benefit). TeensyROM+ playback is unaffected in kind: that link *is*
  byte-bound, and the same model keeps its existing chunking behavior.

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
  flags, so twelve of the twenty-one that the same command honors with
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
- **The books, and the code, are spelled in American English.** `color`,
  `behavior`, `quantise`, `analyser`, `center`, `catalogue`, `licence` and the
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
