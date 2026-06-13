# Ensemble demo

A 3-system video wall (`left`, `middle`, `right`) where the rightmost
system periodically drives a cross-system scrolling big-text broadcast.
The message scrolls right-to-left across all three screens as if they
form one wide canvas.

## Layout

```
+---------+   +---------+   +---------+
|         |   |         |   |         |
|  LEFT   |   | MIDDLE  |   |  RIGHT  |
|         |   |         |   |  (cond) |
+---------+   +---------+   +---------+
   leftmost      middle       rightmost
```

The `[ensemble].systems` array in [`master.toml`](master.toml) is
ordered left-to-right. The rightmost entry is automatically the
conductor for span-mode broadcasts (the message enters from its
right edge and scrolls left across the wall).

## Files

| File                            | Role                                         |
|---------------------------------|----------------------------------------------|
| [`master.toml`](master.toml)    | `[ensemble]` + shared defaults               |
| [`left.toml`](left.toml)        | Leftmost system: clock + callsign idle scene |
| [`middle.toml`](middle.toml)    | Middle system: weather + callsign idle scene |
| [`right.toml`](right.toml)      | Rightmost system: conductor of the broadcast |

Each per-system file is a fully standalone config — you can run
`python -m c64cast --config config/examples/ensemble/left.toml`
to test that system alone before bringing the whole wall up.

## Running

```bash
python -m c64cast --config config/examples/ensemble/master.toml
```

Edit the `[ultimate64].url` in each per-system file first so it
points at your actual U64 IPs. The same `[interstitial]` / `[debug]`
defaults from the master cascade down to each system unless the
per-system TOML overrides them.

## What happens

1. Each system spins up its own playlist on its own thread. They run
   independently — left shows its clock, middle shows its weather,
   right loops its `right-idle` scene.
2. After `right-idle`'s `duration_s` elapses, the rightmost
   playlist advances to `morning-hello`. That scene has
   `orchestrate = true` and a `big_text` overlay; the playlist
   resolves the BigTextSpanOrchestrator and stamps the conductor's
   ensemble state.
3. The big_text overlay's setup publishes the first message's glyph
   bits to the orchestrator and calls `orch.begin(...)`. Every
   follower playlist (left + middle) sees its broadcast-interrupt
   event fire, tears down its current scene, and runs a follower
   big_text scene that renders its 320-pixel slice of the global
   message.
4. The conductor advances its `_scroll_frame` each frame, publishing
   `abs_scroll_px` to the orchestrator. Followers read the
   orchestrator's snapshot each frame to render their slice.
5. When the message has fully scrolled off the leftmost screen
   (`abs_scroll_px >= end_threshold_px`), the conductor advances to
   the next message. After the last message ends, it calls
   `orch.end()`, releasing every follower. Each follower's playlist
   resumes its saved scene index (re-setting up the interrupted
   scene from the start).
6. The rightmost playlist's `morning-hello` scene ends naturally;
   the playlist cycles back to `right-idle`.

The left/middle `morning-hello` scenes are marked
`follower_only = true`, which keeps them out of their own playlist's
rotation — they exist only to be picked up as overrides when the
conductor's broadcast fires. Without that marker, after `left-idle`
expired the leftmost playlist would advance to `morning-hello` and
display its placeholder text standalone.

## Caveats

- All ensemble systems share the same process. A crash in any one
  system's playlist takes the whole process down; designed for a
  single-host video-wall setup, not for fault-isolated deployments.
- Audio: each system has its own `[audio]` config. Two systems
  declaring the same `[audio].device` will fail at hardware-open
  (sounddevice can't open the same input twice).
- Audio coordination: at most one system in the ensemble plays
  audio-bearing content (`commercial`, `waveform`, `midi`) at a time.
  When the slot is held, other systems skip those scenes in their
  playlist and move on to the next entry. Live scenes (`webcam`,
  `blank`) build with audio suppressed in ensemble mode regardless of
  any `audio = true` they declared — they never compete for the SID.
  An ensemble system whose playlist consists entirely of audio-bearing
  scenes will idle until the slot frees; the loader emits a WARNING
  when it spots this configuration.
- Webcam: same. Each system can have its own `[video].device`; two
  systems can't share a camera.
- The control plane is shared across the whole ensemble — set
  `[control] enabled = true` in `master.toml` (NOT in per-system
  files; that section doesn't cascade). Endpoints take a `?system=NAME`
  query param; unscoped POSTs apply to every system.
