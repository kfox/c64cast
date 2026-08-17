"""The C64's screen in the browser, from the machine's own video stream.

Everything else the console does is open-loop. It can author a show, start it,
tune it and save it without ever showing what any of that did — verification
means looking at the television the Commodore is plugged into. For a console
meant to be held in one hand at a gig that was the largest remaining source of
friction, and it made every other improvement worth less than it should be.

The picture comes from the machine, not from c64cast. :mod:`c64cast.hw.vic_stream`
receives the Ultimate 64's own VIC-out UDP stream, so what the browser shows is
what the VIC actually painted rather than what the render pipeline believes it
wrote — which is the difference between a monitor and a second opinion from the
same source. It also means the screen is right for things c64cast did not draw:
a SID scene's own display, the launcher's game, a machine somebody is typing on.

**Ultimate 64 only.** `HardwareProfile.supports_video_stream` is False on an
Ultimate II+, which is a cartridge in someone else's C64 with no VIC to tap, and
on a TeensyROM+, which has neither the FPGA nor the Ethernet MAC this depends
on. Those hosts answer `501` with that as the reason, rather than a blank panel
that could be read as "nothing is running".

## Ref-counted, because the stream is not free

A PAL frame is ~52 KB and the machine sends fifty a second: about 2.6 MB/s of
UDP for as long as it is on. So it is on only while somebody is watching.
:class:`ScreenFeed` counts watchers per system and starts the receiver on the
first and stops it on the last, which is why the acquire/release pair is a
context manager and not two methods anyone could get out of step. The linger is
deliberate: a browser reloading the page drops its connection and makes a new
one a moment later, and tearing the stream down and back up in between would
cost the machine's ARP resolution and a second of black for nothing.

That leaves the process being killed outright, which no `finally` can cover —
handled a layer down by the firmware's own auto-stop timer, which the receiver
re-arms while it is listening. See `vic_stream`.

## PNG, and multipart

`multipart/x-mixed-replace` is the whole client: one `<img>`, no script, no
socket, no decoder, and it works in a browser that has JavaScript turned off.
The alternative — binary frames on the existing WebSocket into a canvas — buys
control the screen does not need and costs a decoder in the page.

**PNG rather than JPEG**, which is the opposite of the usual advice for video
and right here for one reason: this is flat sixteen-colour art with hard edges,
which is the best case for PNG's filters and the worst case for a DCT. A C64
screen is 5-15 KB as PNG, *smaller* than the JPEG that would have ringing
around every character cell.

The frame rate is capped well under the machine's because the point is to see
what a change did, not to relay a demo — and every frame is a compress. What
the cap does not do is slow the machine: it is already sending every frame, and
the ones not encoded are simply the ones no longer in `latest()`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from c64cast._pollthread import PollThread

if TYPE_CHECKING:
    from c64cast.hw.vic_stream import VicFrame

log = logging.getLogger(__name__)

#: Boundary for the `multipart/x-mixed-replace` body. Any token works; this one
#: cannot occur in a PNG.
BOUNDARY = "c64cast-frame"

#: How long a receiver stays up after its last watcher leaves. Long enough to
#: cover a page reload, short enough that a closed tab stops the stream while
#: you are still looking at the tab you closed it from.
LINGER_S = 5.0

#: PNG compression effort. 1 is nearly as small as 9 on flat 16-colour art and
#: several times faster, which is the trade a per-frame encode wants.
_PNG_LEVEL = 1

#: How long a fresh stream is given to produce a frame before the caller is told
#: nothing arrived. Long enough to cover the firmware's destination resolution
#: *and* the receiver's early re-issue of the ON, which is the retry for a cold
#: ARP table — see `vic_stream.PRIME_AFTER_S`. A still asked for before that has
#: elapsed would report "not sending frames yet" about a stream that was one
#: second from working.
_FIRST_FRAME_S = 4.0

#: Re-send the current frame after this long with nothing new. Two reasons, and
#: both are load-bearing. A browser that connects to a *static* screen — a
#: paused show, a BASIC prompt — would otherwise wait for the machine to paint
#: something before it saw anything at all. And the generator only returns
#: control to its driver when it yields, so without this a still screen would
#: block it indefinitely and a client that had gone away would never be
#: noticed, leaving the machine streaming to nobody.
KEEPALIVE_S = 2.0

#: How often the sweeper looks for receivers to retire. Well under `LINGER_S`,
#: so the linger is what decides when a stream ends rather than the polling.
_SWEEP_EVERY_S = 1.0


class ScreenUnavailable(RuntimeError):
    """No picture, and a reason worth showing: the machine can't stream, or
    nothing is running to stream from."""


@dataclass
class _Watched:
    """One system's receiver plus the count of who wants it."""

    receiver: Any
    watchers: int = 0
    idle_since: float = 0.0


@dataclass
class ScreenFeed:
    """Per-system VIC receivers, alive exactly as long as somebody is watching.

    ``backends`` is a provider rather than a map for the reason every registry
    in this layer is: the app outlives the session, so which machines exist is a
    question with a different answer each time it is asked."""

    backends: Callable[[], Mapping[str, Any]]
    _live: dict[str, _Watched] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sweeper: Any = None

    # ---- what a caller can ask for ---------------------------------------

    def available(self) -> dict[str, bool]:
        """Which running systems can show a screen. Empty when nothing runs."""
        return {
            name: bool(getattr(api, "profile", None) and api.profile.supports_video_stream)
            for name, api in self.backends().items()
        }

    def resolve(self, system: str | None) -> str:
        """The system to show, or a `ScreenUnavailable` saying why not one.

        Defaulting to the only running system rather than requiring the name is
        what lets a single-machine console link to the screen without knowing
        what its one system is called."""
        running = self.backends()
        if not running:
            raise ScreenUnavailable("nothing is running, so there is no screen to show")
        if system is None:
            name = next(iter(running))
        elif system in running:
            name = system
        else:
            raise ScreenUnavailable(f"unknown system {system!r}; running: {sorted(running)}")
        api = running[name]
        profile = getattr(api, "profile", None)
        if profile is None or not profile.supports_video_stream:
            raise ScreenUnavailable(
                f"{getattr(profile, 'name', 'this machine')} has no video stream of its own — "
                "the Ultimate 64's FPGA taps its VIC directly, and nothing else here can"
            )
        return name

    def acquire(self, system: str) -> Callable[[], VicFrame | None]:
        """Hold the stream up, and return a way to read frames. Pair with
        :meth:`release` — and prefer :meth:`watching`, which pairs them for you.

        The raw pair exists for one caller: a streaming HTTP response, whose end
        is not the end of any Python block. Tying the release to a `finally`
        inside the frame generator was the first design and was wrong in a way
        that only shows up under a real disconnect — see :meth:`release`."""
        return self._acquire(system).latest

    def release(self, system: str) -> None:
        """Stop wanting the stream. Safe to call more than once per acquire
        only in the sense that the count floors at zero; callers pair it.

        This is what a streaming route hands to Starlette as a background task
        rather than running in the generator's own `finally`. The generator
        runs on a worker thread (it sleeps and encodes), and a client
        disconnecting cancels the async task *while that thread is inside it* —
        closing the generator from there raises `ValueError: generator already
        executing`, the `finally` never runs, and the machine goes on streaming
        to nobody. Found on hardware, by watching it keep streaming."""
        self._release(system)

    @contextmanager
    def watching(self, system: str) -> Iterator[Callable[[], VicFrame | None]]:
        """:meth:`acquire` and :meth:`release` around a block, for every caller
        whose use of the stream *is* a block."""
        read = self.acquire(system)
        try:
            yield read
        finally:
            self.release(system)

    def latest_png(self, system: str) -> bytes:
        """One frame, PNG-encoded — for a caller that wants a still rather than
        a stream, and for anything that cannot render multipart."""
        with self.watching(system) as read:
            frame = _await_frame(read)
            if frame is None:
                raise ScreenUnavailable("the machine is not sending frames yet")
            return encode_png(frame)

    def close(self) -> None:
        """Stop every receiver. Called when the session goes away — the
        watchers are HTTP responses that will notice their stream ended, and a
        machine that has been torn down cannot be told to stop later."""
        with self._lock:
            live, self._live = self._live, {}
            sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.stop()
        for name, watched in live.items():
            _stop_quietly(name, watched.receiver)

    def sweep(self) -> None:
        """Stop receivers nobody is watching, and receivers whose machine is
        gone.

        The second is not the same as the first and is the one that would
        otherwise leak: a show ending takes the backend with it, but a watcher
        still holding the stream open keeps a receiver alive, re-arming a
        watchdog against a link that no longer exists. So "is this system still
        running?" is asked here rather than only "does anyone want it?".

        Called both by :meth:`_sweep_forever` and by anything else already
        ticking (the state feed's push loop) — extra calls are free and the one
        that matters is whichever happens first."""
        now = time.monotonic()
        running = set(self.backends())
        expired = []
        with self._lock:
            for name, watched in list(self._live.items()):
                idle = watched.watchers == 0 and now - watched.idle_since >= LINGER_S
                if idle or name not in running:
                    expired.append((name, self._live.pop(name).receiver))
        for name, receiver in expired:
            _stop_quietly(name, receiver)

    # ---- lifetime ---------------------------------------------------------

    def _acquire(self, system: str) -> Any:
        with self._lock:
            watched = self._live.get(system)
            if watched is not None:
                watched.watchers += 1
                return watched.receiver
        # Built outside the lock: opening it talks to the machine, and holding a
        # process-wide lock across a network round trip is how one slow device
        # stalls every other request.
        receiver = self._open(system)
        with self._lock:
            existing = self._live.get(system)
            if existing is not None:
                # Another request won the race while this one was connecting.
                existing.watchers += 1
                _stop_quietly(system, receiver)
                return existing.receiver
            self._live[system] = _Watched(receiver, watchers=1)
        self._start_sweeper()
        return receiver

    def _start_sweeper(self) -> None:
        """Bring up the thread that expires idle receivers, if it isn't up.

        It has to be a thread of this module's own. The first design leaned on
        the state feed's push loop, on the reasoning that a timer whose only job
        is to notice nothing is happening is a thread paid for at idle — and it
        was wrong in the one case that matters: `/perf` and a bare `<img>` do
        not open a WebSocket, so nothing ticked, nothing swept, and the machine
        went on sending 2.6 MB/s after the last watcher closed the tab. (Found
        on hardware, by watching it keep sending.) Costing nothing at idle is
        preserved by *lifetime* instead: the sweeper exists only while a
        receiver does, and ends itself when the last one goes."""
        with self._lock:
            if self._sweeper is not None:
                return
            self._sweeper = PollThread(
                self._sweep_forever, name="screen-sweeper", manual=True, join_timeout=2.0
            )
        self._sweeper.start()

    def _sweep_forever(self, stop: threading.Event) -> None:
        while not stop.wait(_SWEEP_EVERY_S):
            self.sweep()
            with self._lock:
                if self._live:
                    continue
                self._sweeper = None
            return

    def _open(self, system: str) -> Any:
        api = self.backends().get(system)
        if api is None:
            raise ScreenUnavailable(f"{system} stopped running")
        try:
            receiver = api.open_video_stream()
            receiver.start()
        except Exception as e:  # noqa: BLE001 - every failure here is the same answer
            raise ScreenUnavailable(f"could not start the machine's video stream: {e}") from e
        return receiver

    def _release(self, system: str) -> None:
        with self._lock:
            watched = self._live.get(system)
            if watched is None:
                return
            watched.watchers = max(0, watched.watchers - 1)
            if watched.watchers == 0:
                watched.idle_since = time.monotonic()


def _stop_quietly(system: str, receiver: Any) -> None:
    try:
        receiver.stop()
    except Exception:
        # `%r` rather than `%s` because the name reaches here from a `?system=`
        # query parameter, and the log drawer streams to a browser: a value with
        # a newline in it would arrive looking like a log line of its own.
        # `repr` cannot emit one, so the record holds whatever the caller sent.
        # It is already a validated key of the running-systems map by this point
        # — `_open` refuses an unknown name, and every other call site reads the
        # name out of `_live` — so this is the belt to that braces, and the
        # waiver is for CodeQL modelling neither as a sanitizer. The marker sits
        # on the line it reports — the argument's own — because a suppression on
        # the line above is not one, which cost this alert a second number.
        log.exception("could not stop the stream for %r", system)  # codeql[py/log-injection]


def _await_frame(read: Callable[[], VicFrame | None]) -> VicFrame | None:
    """The first frame, or None if the machine never sent one.

    A fresh stream has nothing to read for a moment — the firmware resolves the
    destination before the first packet — so a caller that looked once would get
    "no picture" from a stream that was about to work."""
    deadline = time.monotonic() + _FIRST_FRAME_S
    while True:
        frame = read()
        if frame is not None:
            return frame
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def encode_png(frame: VicFrame, palette: np.ndarray | None = None) -> bytes:
    """A frame's colour indices as a PNG.

    ``palette`` is BGR rows indexed by colour, defaulting to the host's live
    table — so a host matched to its machine renders the colours that machine
    emits, the same table the swatch picker draws from."""
    if palette is None:
        from c64cast.video.palette import C64_PALETTE_BGR

        palette = C64_PALETTE_BGR
    bgr = np.asarray(palette, dtype=np.uint8)[frame.indices]
    ok, buf = cv2.imencode(".png", bgr, [int(cv2.IMWRITE_PNG_COMPRESSION), _PNG_LEVEL])
    if not ok:
        raise ScreenUnavailable("the frame could not be encoded")
    return bytes(buf)


def multipart_frames(read: Callable[[], VicFrame | None], *, fps: float) -> Generator[bytes]:
    """`multipart/x-mixed-replace` parts, one per frame, forever.

    Ending is the caller's: it closes the generator, which unwinds whatever
    `with` block is holding the machine's stream up. Nothing here polls for a
    departed client, because a plain generator has no way to ask.

    Only *new* frames are encoded. The receiver keeps the latest and nothing
    else, so an unchanged frame number means the machine has not painted
    anything since the last part and re-sending it would be bytes for no
    picture. A still C64 screen therefore costs almost nothing, which is the
    common case for a console left open beside a running show — down to one
    frame every `KEEPALIVE_S`, which is the floor and not zero for the two
    reasons on that constant."""
    period = 1.0 / max(fps, 0.1)
    last: int | None = None
    sent_at = 0.0
    while True:
        frame = read()
        if frame is not None and (
            frame.number != last or time.monotonic() - sent_at >= KEEPALIVE_S
        ):
            last = frame.number
            sent_at = time.monotonic()
            yield png_part(frame)
        time.sleep(period)


def png_part(frame: VicFrame) -> bytes:
    """One `multipart/x-mixed-replace` part carrying a frame as PNG."""
    body = encode_png(frame)
    head = (
        f"--{BOUNDARY}\r\nContent-Type: image/png\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    return head + body + b"\r\n"
