"""mDNS advertisement for the web console host (``--serve``).

Answers "which box on the LAN is a c64cast console, and has it been set up
yet" without a browser first having to be pointed at an IP — the appliance
image's own fixed hostname (``c64cast.local``, set by Armbian + Avahi, not by
this module) answers "where is *the* appliance", while this answers "what is
serving here and in what state", which is the piece a discovery client still
needs when there is more than one box, or none of them has a hostname anyone
picked.

Mirrors :class:`c64cast.wled.wled_device.WledDeviceServer`'s
``_register_mdns``/``_local_ip`` shape — lazy ``zeroconf`` import, a
try/except that logs and gives up rather than raising, the same UDP-connect
trick for the LAN IP — without importing from it: the two live in unrelated
feature areas (a console host vs. a WLED bridge) that neither should depend on
for one 12-line helper, and the WLED module itself carries no shared home for
it either (a third copy already exists in
``c64cast/scenes/overlays/network.py``).

**Advertised only when `[web].host` is not loopback.** ``_advertised_ip()``
names the real LAN-facing address whatever the server is bound to, which is
what a discovery client needs from the appliance's ``0.0.0.0`` — but a console
bound to ``127.0.0.1`` (`WebCfg`'s own default) would then advertise a port
nothing off this machine can reach at all, which is worse than not being
discoverable: a LAN peer that finds it gets a connection refused rather than a
console. A plain ``--serve`` on a laptop therefore stays exactly as quiet on
the network as it always was, with no separate opt-out to configure. The same
reasoning covers the other way of ending up with an unreachable A record:
``_local_ip()`` falling back to loopback because nothing is routable yet also
means there is nothing worth advertising.

``_advertised_ip`` is the one piece with no sibling in the WLED module: a
WLED device is always bound wide, while ``[web].host`` may name one interface
of several and the OS's routing guess need not be that one."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

from c64cast import __version__
from c64cast.app.config import LOOPBACK_HOSTS

log = logging.getLogger(__name__)

#: mDNS service type this console registers itself under. Distinct from
#: `wled_device.WLED_SERVICE_TYPE` on purpose — a discovery client should not
#: have to guess whether a `_wled._tcp` entry is a real WLED device or this
#: bridge impersonating one, and the console is neither.
SERVICE_TYPE = "_c64cast._tcp.local."


#: `_local_ip`'s fallback is a loopback address, and an A record pointing at
#: one is the same "connection refused rather than a console" trap the loopback
#: `host` check below exists to avoid — so it is a reason not to advertise.
LOOPBACK_IPV4_PREFIX = "127."


def _local_ip() -> str:
    """Best-effort primary LAN IPv4 for the mDNS A record. Uses a UDP connect
    trick (no packets are actually sent) so it picks the interface the OS would
    route LAN traffic over, not loopback. Falls back to 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, guaranteed unroutable off-LAN
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _advertised_ip(host: str) -> str:
    """The address to put in the A record for a console bound to `host`.

    `_local_ip()` is a *guess* — the interface the OS would route LAN traffic
    over — and that is exactly right for the appliance's `0.0.0.0`, which is
    listening on every interface anyway. It is the wrong answer when `host`
    names one specific interface: on a multi-homed box the default route need
    not be the one the console actually bound, and advertising the other one
    is the same connection-refused trap this module otherwise avoids. So a
    concrete IPv4 host is advertised as itself, and the guess is reserved for
    the binds it can't be wrong about (`0.0.0.0`) and the ones it can't
    improve on (a hostname, or an IPv6 literal this A record can't carry)."""
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return _local_ip()
    return _local_ip() if address.is_unspecified else str(address)


def _short_hostname() -> str:
    """This machine's hostname with any domain stripped — the DNS-SD instance
    label and the base of the `.local.` name we advertise.

    `socket.gethostname()` is an FQDN on plenty of machines (`c64cast.local` on
    macOS, `box.lan` under some DHCP servers), and only the first label is ours
    to reuse: pasting the whole thing into `f"{name}.local."` yields
    `c64cast.local.local.`, which nothing on the LAN resolves, and the extra
    dots split what should be one instance label into several."""
    return socket.gethostname().split(".")[0] or "c64cast"


def _close_quietly(zc: Any) -> None:
    """Close a `Zeroconf` instance, swallowing whatever teardown says. Every
    caller here is already on a path where the console keeps serving either
    way; what must not happen is the instance's multicast socket and engine
    threads outliving the reference we are dropping."""
    try:
        zc.close()
    except Exception:
        log.debug("web console: mDNS teardown hiccup", exc_info=True)


class ConsoleMdnsAdvertiser:
    """Registers (and tears down) one mDNS `ServiceInfo` for the running
    console. ``start()``/``stop()`` bookend a :func:`c64cast.app.serve.run_daemon`
    loop iteration, like `ControlServer` — a fresh instance every time, so a
    setup completion that flips ``pending`` re-advertises with the new TXT
    record rather than trying to mutate one in place."""

    def __init__(self, host: str, port: int, *, pending: bool) -> None:
        self._host = host
        self._port = port
        self._pending = pending
        self._zc: Any = None
        self._info: Any = None

    def start(self) -> None:
        if self._host in LOOPBACK_HOSTS:
            log.debug("web console: bound to loopback — not advertising over mDNS")
            return
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.debug("web console: zeroconf not installed — not advertising over mDNS")
            return
        ip = _advertised_ip(self._host)
        if ip.startswith(LOOPBACK_IPV4_PREFIX):
            log.debug("web console: no LAN address — not advertising over mDNS")
            return

        zc = None
        try:
            name = _short_hostname()
            zc = Zeroconf()
            info = ServiceInfo(
                SERVICE_TYPE,
                f"{name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],
                port=self._port,
                properties={
                    "md": "c64cast",
                    "ver": __version__,
                    "setup": "1" if self._pending else "0",
                },
                server=f"{name}.local.",
            )
            # allow_name_change, because the appliance case is precisely two
            # boxes flashed from one image sharing a hostname: without it the
            # second one's registration raises `NonUniqueNameException` and it
            # advertises nothing, in the "more than one box" situation this
            # module exists for. Zeroconf renames it (`c64cast-2`) instead.
            zc.register_service(info, allow_name_change=True)
            self._zc = zc
            self._info = info
            log.info("web console: advertised as %r on %s:%d (mDNS)", info.name, ip, self._port)
        except Exception:
            # A discovery failure must not take down the (already-serving)
            # console — it is still reachable by IP:port, just not auto-found.
            # The instance still has to be closed: `stop()` can't reach one we
            # never stored, and its socket and threads would outlive the run.
            log.exception("web console: mDNS advertisement failed (console still serving)")
            if zc is not None:
                _close_quietly(zc)
            self._zc = None
            self._info = None

    def stop(self) -> None:
        zc, info = self._zc, self._info
        self._zc = None
        self._info = None
        if zc is None:
            return
        if info is not None:
            # Its own try: a goodbye packet that fails must not cost us the
            # close that releases the socket.
            try:
                zc.unregister_service(info)
            except Exception:
                log.debug("web console: mDNS unregister hiccup", exc_info=True)
        _close_quietly(zc)
