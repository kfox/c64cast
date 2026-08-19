---
number: B
---

# When Something Goes Wrong

Almost everything in this appendix starts the same way, so it is worth
saying once, at the top:

```bash
c64cast --doctor
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

**Everything is one solid color.** Usually the camera, not c64cast. Confirm
the camera works elsewhere, then check `--list-devices` and pass the right
one with `-d`.

**Banding across gradients.** Turn dithering up, or switch it to
`floyd-steinberg` for still material.

**`mhires` shimmers on moving footage.** Raise `motion_smoothing`. Lower it
again if it starts leaving trails behind fast movement.

**The picture looks washed out.** Check that `auto_fit` has not been turned
off. Modern footage is not graded for a sixteen-color palette and fitting
it first matters more than any other single setting.

**Text or the scrolling message looks blocky or wrong.** c64cast has no
character ROM yet, so it is drawing with a plain built-in font instead of the
Commodore's own. Run `c64cast --doctor --skip-probe` and look at the character
ROM line. If it says *not installed*, connect the Commodore and run
`c64cast --dump-char-rom`, or hand it a copy with
`c64cast --install-char-rom PATH`.

**I turned on `sid_video_mode` and now the capture is torn, rolling, or
black.** Retiming the machine to PAL changes what goes out the HDMI socket,
and some capture devices cannot lock to it. Raise the Ultimate's HDMI scan
resolution — `[ultimate64] hdmi_scan_resolution` defaults to `"auto"`, which
does this for you, but you can pin it to `"HD (720p)"` or `"FullHD (1080p)"`.
The capture device sees the Ultimate's upscaler, not the C64's own timing, and
720p is friendlier than 576p50.

**I use the composite output and retiming made it worse.** Over HDMI the
Ultimate's scaler hides the difference; over composite it does not. c64cast
keeps your color encoding, but the field rate changes with the timing, and a
television built for one standard may not lock to the other's rate. A PAL
machine retimed to NTSC sends PAL color at 60 Hz, which most modern sets cope
with; an NTSC machine retimed to PAL sends NTSC color at 50 Hz, which is
fussier and often comes out in black and white. The sound is unaffected, other
than the pitch change you were after.

If you have no picture at all, hold **C=** and **P** (for PAL) or **C=** and
**N** (for NTSC) while the Ultimate boots. That forces the video mode back to
something you can see. Nothing c64cast changes here is written to the
Ultimate's flash, so switching it off and on again also clears it.

## The Sound Is Wrong

**It sounds rough, quantized, metallic.** If you are on the `$D418` DAC,
that is what a wobbled volume register sounds like, and it is authentic. On a
C64U, setting `[audio] backend = "auto"` uses the much better Ultimate Audio
sampler instead.

**Audio drops in and out.** Usually the link rather than the audio: the
picture and the sound are competing for the same connection. Try a wired
network connection, a lower frame rate, or a character display mode.

**A steady hiss under everything, on a TeensyROM+.** If the TeensyROM+ is in a
C64U or an Ultimate 64, check the Ultimate's **Bus Operation Mode**, which
defaults to **Quiet**. Set it to **Writes**; Chapter 1 has the steps.

**No audio at all.** Check that `[audio] enabled` is true and that you did
not pass `--no-audio`. For microphone input, check that the `mic` feature is
installed and that `-D` names the right device.

**SID tunes play too fast.** Most tunes were written for PAL machines, which
run at about 50 frames a second, but the interrupt the player uses ticks at 60
on either standard — so a PAL tune used to come out nearly twenty percent
quick. `[ultimate64] sid_play_rate` now defaults to `"auto"`, which plays each
tune at its own speed. If you grew up hearing these tunes fast and prefer them
that way, set it to `"off"`.

**SID tunes sound slightly sharp.** That is the processor clock rather than
the tempo — an NTSC machine runs 3.8% faster than a PAL one, which is about
two thirds of a semitone. Fixing it means retiming the machine itself: set
`[ultimate64] sid_video_mode = "auto"` on a C64U or Ultimate 64. Read the note
about capture devices under "The Picture Is Wrong" first, because the HDMI
output mode changes with it.

**Everything looks slightly wrong at once — speed, pitch, frame rate.** That
is `[ultimate64] system` disagreeing with the machine. One setting feeds all of
those, so a wrong value moves them together. Leave it at `"auto"` and c64cast
asks the machine what it is.

## The Playlist Misbehaves

**One scene loops forever and there is no card between scenes.** That is
single-scene mode, and it is deliberate: a configuration with exactly one
scene loops it and skips the interstitial. Add a second scene to get the
normal behavior.

**A scene runs past its `duration_s`.** An overlay is still busy. A
`big_text` message part-way through its scroll defers the transition until
it finishes. Pressing <kbd>CTRL</kbd> always cuts through immediately.

**A `video` scene is rejected when the file loads.** Either the `video`
feature is not installed, or the scene has a `duration_s`, which video
scenes do not accept because they run until the video ends.

**The Commodore keyboard does nothing.** Keyboard control reads the
Commodore's memory, which needs the Web Remote Control Service.

## Installation Trouble

**`c64cast: command not found`.** The install succeeded but its directory is
not on your `PATH`. Run `uv tool update-shell`, then open a new terminal.
Until you do, `uv tool run c64cast` works regardless.

**You upgraded, and `c64cast --version` still shows the old version.** Then
nothing was upgraded. c64cast is a command living in its own environment, not a
folder of files, so unpacking a release archive into a directory leaves a copy of
the source and changes no install at all. `--version` prints the directory the
running code sits in, after the number: upgrade the install it names, then delete
the unpacked copy. See [Upgrading](04-setting-up.md#upgrading).

**A feature says it needs an extra you thought you installed.** You most
likely installed plain `c64cast` rather than `c64cast[all]`. Installing an
extra replaces the whole set rather than adding to it, so name every extra you
want in one command:

```bash
uv tool install --force 'c64cast[all]'
```

`c64cast --doctor` lists which optional features it can see, which settles the
question faster than reading the install output.

**Your editor underlines a setting that c64cast accepts happily.** The editor is
reading a different schema than your install, and the file is fine — nothing
reads the `#:schema` first line when c64cast runs. The usual cause is a line
naming a web address with a version number in it, written when an earlier release
was installed. Run `c64cast --print-schema-path` and put its answer on line 1
with `#:schema ` in front, or let `c64cast --doctor` tell you. See Chapter 2.

**A configuration file works in one directory and not another.** A relative
path inside it — `assets/sids` and the like — is resolved from wherever you
launched c64cast, not from where the file is. Write such paths out in full, or
run from the directory the material sits in. See Chapter 3.

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
collects the hardware behaviors that surprise people, several of which look
exactly like bugs and are not.
