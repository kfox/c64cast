---
number: B
---

# When Something Goes Wrong

Almost everything in this appendix starts the same way, so it is worth
saying once, at the top:

```bash
python -m c64cast --doctor
```

Doctor checks your interpreter, your libraries, your optional features, your
configuration file and your hardware, and reports everything it finds rather
than stopping at the first problem. It is faster than guessing and it is
usually right.

## Nothing Appears At All

**c64cast connects, then hangs.** The Command Interface is switched off. It
lives under **Memory Configuration**, a different menu from the DMA Service's
**Network Settings**, so it is easy to miss; with it off the socket opens but
no command is ever dispatched. Turn both on. See Chapter 1.

**c64cast cannot connect at all.** Either the DMA Service is off, or the
address is wrong, or a firewall is in the way. Check that the address in
your `-u` target matches the one the C64U's own menu shows, and allow TCP to
ports 64 and 80. If the address keeps changing, turn DHCP off and give the
machine a static one (Chapter 1).

If all of that looks right and it still will not connect, reboot the computer
you are running c64cast on before going any further. A stale network
interface, a virtual private network that has half-exited, or a firewall rule
left over from something else will all produce exactly this symptom, and a
restart clears every one of them in one go. It is a dull suggestion and it
works more often than it has any right to.

**Pixels appear, but SID tunes and native programs do not start.** The Web
Remote Control Service is off. Painting the screen and starting a program
are different operations, and only the first goes over the fast path.

## The Picture Is Wrong

**Everything is one solid colour.** Usually the camera, not c64cast. Confirm
the camera works elsewhere, then check `--list-devices` and pass the right
one with `-d`.

**Banding across gradients.** Turn dithering up, or switch it to
`floyd-steinberg` for still material.

**`mhires` shimmers on moving footage.** Raise `motion_smoothing`. Lower it
again if it starts leaving trails behind fast movement.

**The picture looks washed out.** Check that `auto_fit` has not been turned
off. Modern footage is not graded for a sixteen-colour palette and fitting
it first matters more than any other single setting.

## The Sound Is Wrong

**It sounds rough, quantized, metallic.** If you are on the `$D418` DAC,
that is what a wobbled volume register sounds like, and it is authentic. On a
C64U, setting `[audio] backend = "auto"` uses the much better Ultimate Audio
sampler instead.

**Audio drops in and out.** Usually the link rather than the audio: the
picture and the sound are competing for the same connection. Try a wired
network connection, a lower frame rate, or a character display mode.

**No audio at all.** Check that `[audio] enabled` is true and that you did
not pass `--no-audio`. For microphone input, check that the `mic` feature is
installed and that `-D` names the right device.

## The Playlist Misbehaves

**One scene loops forever and there is no card between scenes.** That is
single-scene mode, and it is deliberate: a configuration with exactly one
scene loops it and skips the interstitial. Add a second scene to get the
normal behaviour.

**A scene runs past its `duration_s`.** An overlay is still busy. A
`big_text` message part-way through its scroll defers the transition until
it finishes. Pressing <kbd>Ctrl</kbd> always cuts through immediately.

**A `video` scene is rejected when the file loads.** Either the `video`
feature is not installed, or the scene has a `duration_s`, which video
scenes do not accept because they run until the video ends.

**The Commodore keyboard does nothing.** Keyboard control reads the
Commodore's memory, which needs the Web Remote Control Service.

## Installation Trouble

**An optional feature stays unavailable no matter what you install.** You
almost certainly used `uv pip install`. This project sets a Python toolchain
variable that `uv pip` honours over the project environment, so packages
land where c64cast is not running from. Use `uv sync --all-extras --no-dev`,
and run things with `uv run`.

**Your editor disagrees with what actually runs.** Point your editor's
Python interpreter at `.venv/bin/python` rather than the toolchain
interpreter.

## Getting More Detail

Add `-v` for informational logging, or `-vv` for debug logging including
noisy third-party libraries. Add `--log-file run.log` to keep it. For a
long-running installation, `--heartbeat` prints a periodic line of
throughput statistics, which is the quickest way to tell a slow link from a
slow computer.

## Still Stuck

[`docs/troubleshooting.md`](https://github.com/kfox/c64cast/blob/main/docs/troubleshooting.md)
in the repository is the long version of this appendix, arranged by symptom
and considerably more detailed.
[`docs/caveats.md`](https://github.com/kfox/c64cast/blob/main/docs/caveats.md)
collects the hardware behaviours that surprise people, several of which look
exactly like bugs and are not.
