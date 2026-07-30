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

Expect an acknowledgement within a week. c64cast is a hobby project maintained
by one person, so fixes ship on a best-effort schedule; you will be told which
release carries the fix, and credited in the advisory and the changelog unless
you would rather not be.

## Supported versions

Only the latest released version is supported. While c64cast is `0.x`, fixes go
out in a new release rather than as patches to older ones.

## What c64cast exposes on your network

c64cast is a LAN tool for hardware sitting on your desk. Several of its features
open network listeners, and **none of them authenticate callers** — that is by
design, not an oversight, and it is why none of them belong on an
internet-facing interface. Do not port-forward them, and do not run them on an
untrusted network.

| Surface | Default | Exposure |
|---|---|---|
| HTTP control plane (`[control]`) | off; binds `127.0.0.1:8765` | Any `POST /pause`, `/skip`, `/reload` from anything that can reach the port controls the run. Localhost-only unless you change `host`. |
| Phone/web performance console | off; shares the control-plane server | Rides the same port, so reaching it from a phone means binding the control plane to a LAN address — which exposes the control plane too. |
| WLED bridge Mode 1 (`[wled].listen`) | off; binds `0.0.0.0:8080` when enabled | Presents a virtual WLED device on the LAN, deliberately reachable so the WLED app and Home Assistant can discover it via mDNS. The WLED JSON API has no authentication concept, so anything on the LAN can change scenes and live parameters. |
| WLED audio-sync broadcast (`[wled]` Mode 3) | off | Plaintext UDP to multicast `239.0.0.1:11988`. Carries audio-feature data, not audio. |
| WLED pixel sink (`wled` scene) | off | Accepts an unauthenticated realtime pixel stream (DDP / WLED UDP) from LedFx or xLights. |
| Ultimate 64 / TeensyROM+ link | required | Outbound only. Writes go over the Ultimate DMA Service (TCP 64) and REST; the C64 side has no meaningful access control, so anything that can reach your U64 can already drive it with or without c64cast. |

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
