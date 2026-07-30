# Introduction

Welcome to c64cast. You are about to use one of the most beloved computers
ever made as something it was never designed to be: a programmable display
and audio device, driven live from a modern machine, over a network cable.

The Commodore 64 has a graphics chip called the VIC-II and a sound chip
called the SID. Between them they can show 320×200 pixels in sixteen fixed
colours, subject to some famously awkward rules about how many of those
colours may appear near each other, and they can make three voices of noise.
In 1982 this was remarkable. Today it is a wonderfully specific constraint,
and c64cast exists to feed that constraint with whatever you like: a video
file, a webcam, a photograph, a piece of music, a live MIDI controller, or a
pattern computed from nothing at all.

**c64cast does not emulate anything.** Every C64 screen in this guide was
captured from a real Commodore's own video output, doing real work. Your
computer decodes the
source material, decides how best to express each frame within the VIC-II's
rules, and writes the result directly into the Commodore's memory over the
network, tens of times per second. The Commodore does what it has always
done; it simply has an unusually enthusiastic friend feeding it.

## What You Will Need

To follow this guide you need three things.

**A Commodore 64, with a way in.** c64cast supports two families. The
**Commodore 64 Ultimate** — the C64U, the most modern version of the Ultimate
platform — connects over your network, and everything in this guide applies
to it directly. The **TeensyROM+** connects over USB or over the network, and
the text flags the places where it behaves differently. Chapter 1 sorts out
which older products count as a C64U, and where the Ultimate II+ cartridge
differs.

**A display for it.** Anything the Commodore can already drive. The C64U gives
you HDMI, which is the easiest path.

**A computer to run c64cast on.** macOS or Linux, with a network path to the
Commodore. It does not need to be fast; the heavy work is a few million small
integer operations per frame, which any laptop made this century will manage
comfortably. c64cast installs as a single command and brings its own Python
with it, so you do not need to set one up first. Windows is not yet a tested
platform — the code intends to support it, but nobody has verified the whole
pipeline there.

## Three Words You Will Keep Meeting

Almost everything in c64cast is built from three ideas. They are worth
learning now, because the rest of the guide leans on them constantly.

A **scene** is one thing on the screen: a video playing, a slideshow running,
a SID tune with its oscilloscope, a live webcam feed. A scene knows how to
set itself up, produce frames for a while and tear itself down.

An **overlay** is a decoration stacked on top of a scene: a clock in the
corner, a scrolling message, a spectrum analyser, the current weather. A
scene may carry several overlays at once, and the same overlay works on many
different kinds of scene.

A **playlist** is the running order: which scenes play, for how long, and in
what sequence. It is a plain text file, and building one is the subject of
Chapter 2.

## What This Guide Covers

This guide is arranged so that each chapter needs only what came before it.

**Chapter 1** sets your equipment up properly and gets c64cast talking to
your Commodore reliably, including the diagnostics for when it will not.

**Chapter 2** builds your first playlist, from a single scene to a running
order with several.

**Chapter 3** tours every kind of scene c64cast can run, starting with the
simple ones.

**Chapter 4** is about how a picture actually becomes a C64 picture: display
modes, colour, dithering, and how to stack overlays on top.

**Chapter 5** covers the ambitious end: driving several Commodores at once as
one video wall, playing c64cast live from a MIDI controller, and connecting
it to LED lighting.

The appendices are for looking things up once you know what you are looking
for.

You do not need to read it in order, but the order is not accidental. If you
are new to this, start at Chapter 1 and go forward. Nothing here is difficult,
and none of it is urgent. Take your time.
