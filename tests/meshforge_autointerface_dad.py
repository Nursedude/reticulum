# MeshForge fork test (mf.1): AutoInterface must survive a link-local address
# that is not bindable yet. At boot rnsd can start while IPv6 DAD still holds
# the address "tentative"; the kernel refuses the bind with EADDRNOTAVAIL.
# Stock behaviour (observed 2026-09-26 on VolcanoAI): __init__ adopted the
# interface BEFORE binding, its except said "skipping it" but left the
# adoption in place, and final_init() re-bound the same address unguarded ->
# unhandled OSError -> rnsd exit 255.
#
# Contract pinned here:
#   1. EADDRNOTAVAIL is retried, bounded by LINKLOCAL_BIND_TIMEOUT.
#   2. Any other errno propagates at once (no retry masks a real fault).
#   3. A bind that stays unassignable rolls the adoption back, so "skipping
#      it" is true and final_init() binds nothing for that interface.
import errno
import socket
import unittest
from unittest import mock

import RNS  # noqa: F401
from RNS.Interfaces import netinfo
from RNS.Interfaces.AutoInterface import AutoInterface

# fe80::/64 on loopback is never assigned, so binding it is a REAL kernel
# EADDRNOTAVAIL — the same refusal a tentative address gets. Not a mock.
UNASSIGNED_LL = "fe80::dead:beef:1"
LO = "lo"


def _addrs(ifname):
    return {netinfo.AF_INET6: [{"addr": UNASSIGNED_LL + "%" + ifname}]}


class BindWhenReadyTest(unittest.TestCase):
    def setUp(self):
        self.iface = AutoInterface.__new__(AutoInterface)
        self.sleeps = mock.patch("RNS.Interfaces.AutoInterface.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_retries_eaddrnotavail_then_succeeds(self):
        calls = []

        def bind():
            calls.append(1)
            if len(calls) < 3:
                raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
            return "bound"

        self.assertEqual(self.iface._bind_when_ready(bind, "t"), "bound")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps.call_count, 2)

    def test_other_errno_is_not_retried(self):
        def bind():
            raise OSError(errno.EADDRINUSE, "Address already in use")

        with self.assertRaises(OSError) as cm:
            self.iface._bind_when_ready(bind, "t")
        self.assertEqual(cm.exception.errno, errno.EADDRINUSE)
        self.sleeps.assert_not_called()

    def test_gives_up_after_the_bound(self):
        with mock.patch.object(AutoInterface, "LINKLOCAL_BIND_TIMEOUT", 0.0):
            with self.assertRaises(OSError) as cm:
                self.iface._bind_when_ready(
                    lambda: (_ for _ in ()).throw(OSError(errno.EADDRNOTAVAIL, "x")), "t")
        self.assertEqual(cm.exception.errno, errno.EADDRNOTAVAIL)

    def test_real_kernel_refusal_is_eaddrnotavail(self):
        # Guards the premise: the errno we retry on is the one the kernel gives.
        idx = socket.if_nametoindex(LO)
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(OSError) as cm:
                s.bind((UNASSIGNED_LL, 0, 0, idx))
        finally:
            s.close()
        self.assertEqual(cm.exception.errno, errno.EADDRNOTAVAIL)


class HalfAdoptionRollbackTest(unittest.TestCase):
    def _build(self):
        # Real __init__, real sockets, real kernel refusal; only interface
        # enumeration is steered at loopback + an address it does not hold.
        with mock.patch.object(AutoInterface, "LINKLOCAL_BIND_TIMEOUT", 0.2, create=True), \
             mock.patch.object(AutoInterface, "LINKLOCAL_BIND_RETRY", 0.05, create=True), \
             mock.patch.object(AutoInterface, "list_interfaces", lambda self: [LO]), \
             mock.patch.object(AutoInterface, "list_addresses", lambda self, n: _addrs(n)), \
             mock.patch.object(RNS.Reticulum, "get_instance", return_value=mock.MagicMock()):
            return AutoInterface(mock.MagicMock(), {"name": "Auto", "devices": LO})

    def test_unbindable_interface_is_not_left_adopted(self):
        iface = self._build()
        self.assertEqual(iface.adopted_interfaces, {})
        self.assertEqual(iface.link_local_addresses, [])
        self.assertNotIn(LO, iface.multicast_echoes)

    def test_final_init_does_not_rebind_a_skipped_interface(self):
        iface = self._build()
        with mock.patch("RNS.Interfaces.AutoInterface.socketserver.UDPServer") as udp, \
             mock.patch("RNS.Interfaces.AutoInterface.threading.Thread"), \
             mock.patch("RNS.Interfaces.AutoInterface.time.sleep"):
            iface.final_init()
        udp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
