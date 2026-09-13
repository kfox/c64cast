"""mDNS advertisement for the web console host (``--serve``).

Registers one ``_c64cast._tcp.local.`` service per
:func:`c64cast.app.serve.run_daemon` loop iteration, carrying a TXT record a
discovery client can read as "configured" or "still in the setup window".
Mirrors :class:`c64cast.wled.wled_device.WledDeviceServer`'s
``_register_mdns``/``_local_ip`` shape without importing from it.

Advertised only when ``[web].host`` is not loopback and there is a LAN address
to name — an A record nothing is listening on is worse than no entry at all.

See docs/architecture/control.md#console_mdnspy--mdns-advertisement-of-the-web-console.
"""

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

    A concrete IPv4 host is advertised as itself; `_local_ip()`'s routing
    guess is reserved for the binds it cannot be wrong about (`0.0.0.0`) and
    the ones it cannot improve on (a hostname, or an IPv6 literal this A
    record cannot carry)."""
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return _local_ip()
    return _local_ip() if address.is_unspecified else str(address)


def _short_hostname() -> str:
    """This machine's hostname with any domain stripped — the DNS-SD instance
    label and the base of the `.local.` name we advertise.

    `socket.gethostname()` is an FQDN on plenty of machines (`c64cast.local` on
    macOS, `box.lan` under some DHCP servers), and pasting the whole thing into
    `f"{name}.local."` yields a name nothing on the LAN resolves."""
    return socket.gethostname().split(".")[0] or "c64cast"


def _close_quietly(zc: Any) -> None:
    """Close a `Zeroconf` instance, swallowing whatever teardown says. What
    must not happen is the instance's multicast socket and engine threads
    outliving the reference being dropped."""
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
            # allow_name_change: two boxes flashed from one image share a
            # hostname, and without it the second registration raises
            # NonUniqueNameException and advertises nothing at all.
            zc.register_service(info, allow_name_change=True)
            self._zc = zc
            self._info = info
            log.info("web console: advertised as %r on %s:%d (mDNS)", info.name, ip, self._port)
        except Exception:
            # The instance still has to be closed: `stop()` cannot reach one
            # that was never stored, and its socket and threads would outlive
            # the run.
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
