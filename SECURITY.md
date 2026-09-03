# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Use GitHub's
private vulnerability reporting instead:

**<https://github.com/kfox/c64cast/security/advisories/new>**

That opens a private advisory visible only to the maintainer. If GitHub
reporting is unavailable to you, mail **c64cast@gmail.com** instead.

Include what you
were running (`c64cast --doctor` output is ideal), which network surface is
involved, and what an attacker gains. If you have a proof of concept, attach it
there rather than posting it publicly.

Expect an acknowledgment within a week. c64cast is a hobby project maintained
by one person, so fixes ship on a best-effort schedule; you will be told which
release carries the fix, and credited in the advisory and the changelog unless
you would rather not be.

## Supported versions

Only the latest released version is supported. While c64cast is `0.x`, fixes go
out in a new release rather than as patches to older ones.

## What c64cast exposes on your network

c64cast is a LAN tool for hardware sitting on your desk. Several of its features
open network listeners, and **all but one of them authenticate no callers** —
that is by design, not an oversight, and it is why none of them belong on an
internet-facing interface. Do not port-forward them, and do not run them on an
untrusted network. Two of them can be locked: the HTTP control plane can be put
behind a shared token, and the web console host is *always* behind one (below).
That is a lock on the door, not a reason to expose the port.

| Surface | Default | Exposure |
|---|---|---|
| HTTP control plane (`[control]`) | off; binds `127.0.0.1:8765`; **no token** | Unauthenticated by default: any `POST /pause`, `/skip`, `/reload` from anything that can reach the port controls the run. Localhost-only unless you change `host`. Setting `[control].token` (or `$C64CAST_CONTROL_TOKEN`) requires that token on every route, including the console and its WebSocket; `viewer_token` grants reads only. |
| Web console host (`--serve` / `[web]`) | off; binds `127.0.0.1:8123`; **always token-gated** | The only surface that starts and stops hardware on request, so it has no unauthenticated mode: with no token configured, one is generated, stored `0600` under the data directory, and printed at startup. The control-plane routes and the performance console ride the same port and the same token. The browser console it serves is behind the same gate — no page, script or stylesheet of it is public, and an unauthenticated navigation gets a form asking for the token rather than the application. `viewer_token` grants reads only. A full token is remote control of the machine — the sessions it starts open whatever media paths and URLs the configuration names. |
| Appliance setup window (`[web].setup_wizard`) | **off** | The one deliberate exception to the row above, and off by default — a pre-provisioned OS image is the only intended caller, never a console you configure yourself. While pending, `/api/setup` (connection target + token choice) and the console shell that draws its form are reachable with no token at all, and everything else answers `503` rather than reaching any hardware/config/media route. The form is never told the host's token; it learns it only by completing setup. See the note below on how narrow that is and when it closes. |
| Console mDNS advertisement (`control/console_mdns.py`) | on whenever `--serve` binds a non-loopback `host` | No new route — it announces one that already exists. A `_c64cast._tcp` browse on the LAN sees this host's hostname, IP, and port, plus a TXT record naming the c64cast version and whether the appliance setup window above is still open. That last bit is the one worth weighing: it tells anything listening which boxes on the LAN still have their setup window open, same LAN as the window itself already trusts. Silent (no `zeroconf` import attempted) while `host` is loopback, which is `[web]`'s own default — an ordinary `--serve` on a laptop advertises nothing unless you deliberately open it to the network. |
| Web console config browser (`[web].config_roots`) | the directory the host was launched from | The only surface that reads and writes files on the host. Confined to the configured roots (resolved, so a symbolic link out of one is refused) and to `.toml` names, and a write must load before it lands. A full token is required — `viewer_token` cannot write. See the note below on what that access is actually worth. |
| Web console media picker + uploader (`[web].media_read_write` / `media_read_only`) | the four default asset directories, uploadable | Confined to the configured roots the same way the config browser is (resolved, symlinks included). `media_read_write` directories are both browsable and a valid upload destination; `media_read_only` adds browse-only directories. An upload's name must be a bare filename with an extension a known media kind ends in, is capped at 512 MiB, and is never overwritten — a name already taken is renamed rather than replacing what's there. Nothing is ever deleted here. A full token is required to upload; a `viewer` token may still browse, same as it may watch the screen. |
| Phone/web performance console | off; shares the control-plane server | Rides the same port, so reaching it from a phone means binding the control plane to a LAN address — which exposes the control plane too, under the same token or the same absence of one. |
| WLED bridge Mode 1 (`[wled].listen`) | off; binds `0.0.0.0:8080` when enabled | Presents a virtual WLED device on the LAN, deliberately reachable so the WLED app and Home Assistant can discover it via mDNS. The WLED JSON API has no authentication concept, so anything on the LAN can change scenes and live parameters. |
| WLED audio-sync broadcast (`[wled]` Mode 3) | off | Plaintext UDP to multicast `239.0.0.1:11988`. Carries audio-feature data, not audio. |
| WLED pixel sink (`wled` scene) | off | Accepts an unauthenticated realtime pixel stream (DDP / WLED UDP) from LedFx or xLights. |
| Ultimate 64 / TeensyROM+ link | required | Outbound only. Writes go over the Ultimate DMA Service (TCP 64) and REST; the C64 side has no meaningful access control, so anything that can reach your U64 can already drive it with or without c64cast. |

The control-plane token is a shared secret sent over plain HTTP, so it is
readable by anything that can watch the traffic on your network. It stops a
housemate's laptop and a curious phone, not an attacker on the wire. It is
supplied through `$C64CAST_CONTROL_TOKEN` / `$C64CAST_CONTROL_VIEWER_TOKEN` or
the `[control].token` / `viewer_token` config keys, and has **no CLI flag** for
the same reason the DMA password doesn't. c64cast warns when the control plane
binds a non-loopback address with no token, but does not refuse — the surface
predates the token, and breaking those runs is not the token's business.

The web console's token works the same way and is supplied the same way
(`$C64CAST_WEB_TOKEN` / `$C64CAST_WEB_VIEWER_TOKEN`, `[web].token` /
`token_file` / `viewer_token`, no CLI flag). The difference is that it has no
"off": that surface has no history to preserve and it owns the hardware, so a
host with no token configured mints one rather than binding open.

**If you put a reverse proxy in front of this, suppress query strings from its
access log.** A token legitimately rides in a URL in three places — the login
link the daemon prints at startup, the read-only link the console hands out,
and the `?token=` escape hatch for `curl` — because a browser can set no
headers on a plain navigation or a WebSocket handshake. c64cast's own logging
handles that (uvicorn runs with `access_log=False`, and the log the console
shows redacts on the way in), but nothing it does can reach nginx's
`$request_uri`. Nothing here needs a proxy; this is for the deployments that
add one anyway.

**A token you set by hand should be long.** Neither login route nor the gate
throttles attempts, and nothing refuses a short token — `[web].token = "c64"`
is a console that falls to a few thousand unanswered requests. A generated one
is 32 URL-safe bytes; c64cast warns below 16 characters and otherwise honors
what you configured.

**The setup window's exposure is bounded by construction, not by an allowlist.**
`setup_gate.py` blocks every route the app already knows about except the
console's own static assets and `/api/setup` itself — nothing that starts
hardware, reads a config, or browses media is reachable while it is pending,
and it closes the moment the form is submitted with a valid connection
target: the process rebuilds its app from scratch, without the exemption or
the gate, before serving another request. Whoever reaches the form first on
the LAN configures the box, the same trust model a home router's first boot
uses — this is a real widening of what an unauthenticated LAN peer can do
(name where the console talks to next) for as long as the window is open, and
is the reason it is off unless something has deliberately turned it on.
`c64cast --reset-setup` reopens it; there is no HTTP equivalent, on purpose —
anyone who can already run that command has shell access to the box, and a
route that did the same thing over HTTP would hand that reopening power to
anyone who could merely reach the port.

**A lost data directory does not reopen it.** The completion marker lives
under the data root (`~/.local/share/c64cast/setup.json` by default), and its
absence used to be the only evidence consulted — so a data root that is a
container layer with no volume, a tmpfs, or a swept cache reopened the window
on a host that was still fully configured, since machine settings live under
the *config* dir instead. The window now also requires that machine settings
not already name a connection target; a provisioned host with a missing
marker logs a warning and stays shut, and `--reset-setup` writes an explicit
reopen marker beside removing the completion one so an admin with shell
access can still ask for it. Opening the window logs a warning, every time.

**Nothing the window exposes leaks the token.** `GET /api/setup` reports only
whether a token may be *set* — never the token, redacted or otherwise, because
that route answers anyone on the LAN for as long as the window is open. The
full token is handed back exactly once, in the login link of a *completed*
setup, to the caller who just configured the box; that is the same trust
decision the paragraph above describes, and on an appliance with no terminal it
is the only way an admin ever learns it. A token named by `[web].token`,
`[web].token_file` or `$C64CAST_WEB_TOKEN` cannot be replaced through the form
at all: those outrank the file the form would write, so accepting one would
answer "ok" and then lock the admin out on the next restart.

**Treat a full web-console token as shell-equivalent on that host.** The root
list bounds *which files the browser may edit*, not what a saved file can then
reach: a configuration names media paths and URLs that a session will open and
that `yt-dlp` will fetch. Confining the editor is worth doing — it is why
`config_roots` defaults to one directory rather than the whole filesystem — but
it is a blast-radius limit on the editing, not a sandbox around the run.

The Ultimate's optional DMA password is supplied through the
`C64CAST_DMA_PASSWORD` environment variable or the `[ultimate64].dma_password`
config key, and deliberately has **no CLI flag** so it cannot land in shell
history or in `ps` output. `--save-settings` and the config serializer refuse to
write it to disk. Treat it as a weak gate against accidents rather than a
security boundary.

## Handling of media and network content

c64cast decodes media you point it at (video, images, `.sid` files) with
OpenCV, PyAV/FFmpeg, and NumPy, and fetches remote content for the RSS, weather,
and URL-playback features. A malicious file or feed is handled by those
libraries' parsers, not by c64cast's own code — keep the optional dependencies
current, `yt-dlp` especially. That is why `yt-dlp` is the one dependency with no
version ceiling: it is network-facing, and running a recent release is part of
its security posture.

The `launcher` scene hands the machine over to a native `.prg`/`.crt`, which
then runs unrestricted on the C64. That is 6502 code on real hardware, outside
anything c64cast can sandbox. Run programs you trust.
