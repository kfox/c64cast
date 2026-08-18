# Performing

## Clips

A clip is a scene fired from a pad. It takes every key an ordinary scene takes,
plus how it launches.

```toml
[[performance.clips]]
slot = 1
pad = 60
type = "generative"
source = "tunnel"
launch = "trigger"
quantize = "bar"
```

| `launch` | Behavior |
|---|---|
| `trigger` | Plays through, then loops |
| `gate` | Plays while held, restores what it interrupted |
| `toggle` | Latches on and off |

| `quantize` | Fires |
|---|---|
| `off` | At once |
| `beat` / `bar` | At the next beat or bar |

Building a clip costs real setup time, so a press starts that work at once and
the count-in hides it. A launch remembers what it interrupted, one level deep. A
stopped clock fires immediately, so a pad always does something.

## Tempo

| `[performance].tempo_source` | The grid follows |
|---|---|
| `internal` | `[performance].bpm`, re-anchored by a `tempo_tap` pad |
| `midi` | External MIDI clock, on `clock_port` if not the control port |
| `audio` | The tempo detected on a `mic` or `listen` scene |

Phase is integrated from the tempo, never snapped, so it cannot jerk backward
when a clock byte lands late, and silence freezes the `audio` grid. Launch
quantization, effects with `mod_source = "clock"` and the WLED broadcast under
`broadcast_tempo_fallback` all consume it.

## Looks

A look is the active clip plus the on-screen scene's whole effect-chain state:
what is bypassed, what each parameter is set to, and what modulates it.
`look_save` captures a slot, `look_recall` re-fires it. Recalled onto a
differently built chain, it applies what it can by layer index.

## The Console

Two pages, both served by c64cast. `[control].enabled` and the `control` extra
serve `127.0.0.1:8765`.

| Route | Does |
|---|---|
| `GET /perf` | The phone console: clips, rack, tune, tempo, transport, looks, jump |
| `GET /status` | Scene, index, paused, write latency |
| `GET /scenes` | The playlist, and which scene is live |
| `POST /pause`, `/resume`, `/skip` | What the keyboard does |
| `POST /reload` | Rebuild the playlist at the next scene boundary |

Every route takes `?system=` naming one system, or `all`. Nothing on the console
reaches the audience's screen.

`c64cast --serve` and the `web` extra serve the full browser console on
`127.0.0.1:8123` instead — a host that holds the Commodore across shows.

| On the Live screen | Does |
|---|---|
| **Screen** | The C64's own picture, tapped off the VIC by the U64's FPGA. **Watch** starts it, **Stop** ends it. U64 only |
| Tempo bar | Tap tempo, pause, resume, skip |
| **Scenes** | The playlist, with a tap to jump — a cut, no interstitial |
| **Tune** | The running scene's colour, generator and scope knobs |
| **Keep** | Writes what you tuned into the config the show is running from |

It prints a login URL holding its own token at startup; that token can stop the
show, so hand out the read-only link from **Session** instead. `[web].screen_fps
= 0` turns the picture off. `/perf` needs no bundle — reach for it if `make web`
was never run.

## WLED

| Direction | Setting | Does |
|---|---|---|
| Out | `[wled].broadcast` | Fixtures react to the music, with no microphone |
| In | `[wled].listen` | The WLED app or Home Assistant drives c64cast |
| In | A `wled` scene | LedFx, xLights or Jinx! streams pixels to the Commodore |

Listening, the mapping is a pun on WLED's own vocabulary: power is pause,
brightness is a real screen dim, the playlist is the effect list, color forces
a palette, and speed and intensity are the scene's live parameters.

## Several Commodores

The control plane, the MIDI listener and the WLED device are process-wide: one
of each for the whole wall.

| Surface | Addresses a system by |
|---|---|
| MIDI | Channel *N* is system *N* in ensemble order; `broadcast_channel` (16) is all |
| WLED | A segment, in ensemble order |
| Console | A tab per system |

At most one system may sound at a time, and a system that cannot claim the audio
slot skips to its next quiet scene.

## Before the Show

| Command | Does |
|---|---|
| `c64cast --list-devices` | Cameras and audio inputs, with identifiers |
| `c64cast --midi-setup` | Learn a controller, write a reusable profile |
| `c64cast --save-settings` | Persist this machine's URL, devices, SID model |
| `c64cast --doctor --skip-probe` | Check the environment and the config, offline |
