"""Tests for the web console's mDNS advertisement.

`zeroconf` is faked via a `sys.modules` patch (mirroring
`test_dac_calibration.py`'s `sounddevice` fakes) rather than exercised for
real: a genuine `Zeroconf()` opens a multicast socket, which is exactly the
kind of network dependency a unit test must not have."""

from __future__ import annotations

import socket
import sys
import unittest
from unittest import mock

from c64cast import __version__
from c64cast.control import console_mdns


class _FakeServiceInfo:
    def __init__(self, type_, name, *, addresses, port, properties, server):
        self.type = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.properties = properties
        self.server = server


class _FakeZeroconf:
    def __init__(self):
        self.registered: list[_FakeServiceInfo] = []
        self.unregistered: list[_FakeServiceInfo] = []
        self.name_change_allowed: list[bool] = []
        self.closed = False

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)
        self.name_change_allowed.append(allow_name_change)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


class _RaisingZeroconf(_FakeZeroconf):
    def register_service(self, _info, allow_name_change=False):
        raise RuntimeError("boom")


class _UnregisterRaisingZeroconf(_FakeZeroconf):
    def unregister_service(self, _info):
        raise RuntimeError("goodbye packet failed")


def _fake_module(zeroconf_cls=_FakeZeroconf):
    """A stand-in `zeroconf` module, plus the list of `Zeroconf` instances it
    hands out — a test that has to inspect one `start()` created and then
    dropped (the failure path) can't reach it through the advertiser."""
    made: list[_FakeZeroconf] = []

    def factory():
        zc = zeroconf_cls()
        made.append(zc)
        return zc

    module = mock.Mock()
    module.Zeroconf = factory
    module.ServiceInfo = _FakeServiceInfo
    return module, made


class LoopbackTest(unittest.TestCase):
    def test_a_loopback_host_never_imports_zeroconf(self):
        # sys.modules["zeroconf"] = None makes the import raise ImportError,
        # so anything short of a true early-return here would blow up.
        with mock.patch.dict(sys.modules, {"zeroconf": None}):
            advertiser = console_mdns.ConsoleMdnsAdvertiser("127.0.0.1", 8123, pending=False)
            advertiser.start()
        self.assertIsNone(advertiser._zc)

    def test_stop_before_start_is_a_no_op(self):
        console_mdns.ConsoleMdnsAdvertiser("127.0.0.1", 8123, pending=False).stop()


class MissingDependencyTest(unittest.TestCase):
    def test_a_missing_zeroconf_is_reported_at_debug_and_does_not_raise(self):
        with mock.patch.dict(sys.modules, {"zeroconf": None}):
            advertiser = console_mdns.ConsoleMdnsAdvertiser("0.0.0.0", 8123, pending=False)
            with self.assertLogs("c64cast.control.console_mdns", level="DEBUG") as cm:
                advertiser.start()
        self.assertTrue(any("not advertising" in m for m in cm.output))
        self.assertIsNone(advertiser._zc)


class _AdvertiserTestCase(unittest.TestCase):
    """Base for the tests that get as far as registering. `_local_ip` is pinned
    rather than called: the real one opens a UDP socket and answers with
    whatever address this machine happens to have, which decides whether
    `start()` advertises at all (see `NoLanAddressTest`). These bind `0.0.0.0`,
    so it is the address `_advertised_ip` hands on."""

    LAN_IP = "10.1.2.3"

    def setUp(self):
        patcher = mock.patch.object(console_mdns, "_local_ip", return_value=self.LAN_IP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def advertise(self, *, pending=False, zeroconf_cls=_FakeZeroconf, hostname="c64cast"):
        module, made = _fake_module(zeroconf_cls)
        with mock.patch.dict(sys.modules, {"zeroconf": module}):
            with mock.patch.object(socket, "gethostname", return_value=hostname):
                advertiser = console_mdns.ConsoleMdnsAdvertiser("0.0.0.0", 8123, pending=pending)
                advertiser.start()
        return advertiser, made


class RegistrationTest(_AdvertiserTestCase):
    def test_a_non_loopback_host_registers_a_service_with_the_expected_shape(self):
        advertiser, _ = self.advertise(pending=True)

        zc = advertiser._zc
        self.assertIsInstance(zc, _FakeZeroconf)
        self.assertEqual(len(zc.registered), 1)
        info = zc.registered[0]
        self.assertEqual(info.type, console_mdns.SERVICE_TYPE)
        self.assertEqual(info.name, f"c64cast.{console_mdns.SERVICE_TYPE}")
        self.assertEqual(info.server, "c64cast.local.")
        self.assertEqual(info.addresses, [socket.inet_aton(self.LAN_IP)])
        self.assertEqual(info.port, 8123)
        self.assertEqual(info.properties["md"], "c64cast")
        self.assertEqual(info.properties["ver"], __version__)
        self.assertEqual(info.properties["setup"], "1")

    def test_setup_completed_is_reported_as_0(self):
        advertiser, _ = self.advertise(pending=False)

        self.assertEqual(advertiser._zc.registered[0].properties["setup"], "0")

    def test_a_name_clash_with_another_box_is_left_to_zeroconf_to_resolve(self):
        # Two appliances flashed from one image share a hostname; without this
        # the second one's registration raises and it advertises nothing.
        advertiser, _ = self.advertise()

        self.assertEqual(advertiser._zc.name_change_allowed, [True])

    def test_an_fqdn_hostname_is_reduced_to_its_first_label(self):
        # `gethostname()` answers with an FQDN on plenty of machines. Using it
        # whole would advertise the unresolvable server name `box.lan.local.`.
        advertiser, _ = self.advertise(hostname="box.lan")

        info = advertiser._zc.registered[0]
        self.assertEqual(info.name, f"box.{console_mdns.SERVICE_TYPE}")
        self.assertEqual(info.server, "box.local.")

    def test_stop_unregisters_and_closes(self):
        advertiser, _ = self.advertise()
        zc = advertiser._zc
        advertiser.stop()

        self.assertEqual(zc.unregistered, zc.registered)
        self.assertTrue(zc.closed)
        self.assertIsNone(advertiser._zc)
        self.assertIsNone(advertiser._info)

    def test_stop_is_idempotent(self):
        advertiser, _ = self.advertise()
        advertiser.stop()
        advertiser.stop()  # must not raise

    def test_a_failed_goodbye_still_closes_the_instance(self):
        advertiser, _ = self.advertise(zeroconf_cls=_UnregisterRaisingZeroconf)
        zc = advertiser._zc
        with self.assertLogs("c64cast.control.console_mdns", level="DEBUG") as cm:
            advertiser.stop()

        self.assertTrue(any("unregister hiccup" in m for m in cm.output))
        self.assertTrue(zc.closed, "a failed goodbye packet leaked the multicast socket")


class NoLanAddressTest(_AdvertiserTestCase):
    """`_local_ip`'s loopback fallback means nothing off this machine can reach
    the port, which is the same reason a loopback `host` isn't advertised."""

    LAN_IP = "127.0.0.1"

    def test_a_loopback_local_ip_is_not_advertised(self):
        with self.assertLogs("c64cast.control.console_mdns", level="DEBUG") as cm:
            advertiser, made = self.advertise()

        self.assertTrue(any("no LAN address" in m for m in cm.output))
        self.assertEqual(made, [], "a Zeroconf instance was opened anyway")
        self.assertIsNone(advertiser._zc)


class RegistrationFailureTest(_AdvertiserTestCase):
    def test_a_registration_failure_is_logged_and_does_not_raise(self):
        with self.assertLogs("c64cast.control.console_mdns", level="ERROR") as cm:
            advertiser, _ = self.advertise(zeroconf_cls=_RaisingZeroconf)  # must not raise

        self.assertTrue(any("mDNS advertisement failed" in m for m in cm.output))
        self.assertIsNone(advertiser._zc)
        self.assertIsNone(advertiser._info)
        advertiser.stop()  # must not raise either

    def test_a_registration_failure_closes_the_instance_it_opened(self):
        # `stop()` cannot reach an instance `start()` never stored, so a
        # failure that merely drops the reference leaks its multicast socket
        # and engine threads — once per `run_daemon` restart, at that.
        with self.assertLogs("c64cast.control.console_mdns", level="ERROR"):
            _, made = self.advertise(zeroconf_cls=_RaisingZeroconf)

        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].closed, "the failed advertisement leaked a Zeroconf instance")


class LocalIpTest(unittest.TestCase):
    def test_falls_back_to_loopback_on_a_socket_error(self):
        with mock.patch.object(socket.socket, "connect", side_effect=OSError("no route")):
            self.assertEqual(console_mdns._local_ip(), "127.0.0.1")


class AdvertisedIpTest(unittest.TestCase):
    """A console bound to one specific interface advertises *that* one. The
    routing guess can name a different interface on a multi-homed box — one the
    console isn't listening on at all."""

    GUESS = "10.9.9.9"

    def setUp(self):
        patcher = mock.patch.object(console_mdns, "_local_ip", return_value=self.GUESS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_concrete_ipv4_bind_is_advertised_as_itself(self):
        self.assertEqual(console_mdns._advertised_ip("192.168.5.10"), "192.168.5.10")

    def test_a_wildcard_bind_falls_back_to_the_routing_guess(self):
        self.assertEqual(console_mdns._advertised_ip("0.0.0.0"), self.GUESS)

    def test_a_hostname_bind_falls_back_to_the_routing_guess(self):
        self.assertEqual(console_mdns._advertised_ip("console.lan"), self.GUESS)

    def test_an_ipv6_bind_falls_back_to_the_routing_guess(self):
        # The A record this module builds can't carry one.
        self.assertEqual(console_mdns._advertised_ip("::"), self.GUESS)


class ShortHostnameTest(unittest.TestCase):
    def test_a_bare_hostname_is_used_as_is(self):
        with mock.patch.object(socket, "gethostname", return_value="c64cast"):
            self.assertEqual(console_mdns._short_hostname(), "c64cast")

    def test_an_empty_hostname_falls_back_to_the_project_name(self):
        with mock.patch.object(socket, "gethostname", return_value=""):
            self.assertEqual(console_mdns._short_hostname(), "c64cast")


if __name__ == "__main__":
    unittest.main()
