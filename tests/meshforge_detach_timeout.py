# MeshForge fork test (rnsd-SIGTERM): detach_interfaces() must be bounded so a
# busy transport node's SIGTERM teardown cannot hang the main thread. Upstream
# did an unbounded dt.join() plus a synchronous local-client and shared-instance
# detach() — if any interface's detach() blocks, RNS.exit() is never reached and
# systemd waits the full TimeoutStopSec before SIGKILL. The fix bounds the whole
# detach to Transport.DETACH_TIMEOUT.
import threading
import time
import unittest
from unittest import mock

import RNS  # noqa: F401
from RNS.Transport import Transport
from RNS.Interfaces.LocalInterface import LocalServerInterface, LocalClientInterface


def _blocking_iface(cls=None):
    """A fake interface whose detach() blocks forever. If cls is given, the
    object's exact type() is cls (so Transport's `type(x) == LocalServerInterface`
    branch selection matches), built via __new__ to skip the heavy __init__."""
    iface = mock.MagicMock() if cls is None else cls.__new__(cls)
    iface.detached = False
    started = threading.Event()

    def _block():
        started.set()
        threading.Event().wait()  # never returns

    iface.detach = _block
    iface._mf_started = started
    return iface


class MeshForgeDetachTimeoutTest(unittest.TestCase):
    def setUp(self):
        # Snapshot the class-level Transport state these tests mutate.
        self._saved = {
            "active_links": Transport.active_links,
            "pending_links": Transport.pending_links,
            "interfaces": Transport.interfaces,
            "local_client_interfaces": Transport.local_client_interfaces,
            "DETACH_TIMEOUT": Transport.DETACH_TIMEOUT,
        }
        Transport.active_links = []
        Transport.pending_links = []
        Transport.interfaces = []
        Transport.local_client_interfaces = []
        Transport.DETACH_TIMEOUT = 0.3  # keep the bound-proving tests fast
        # deregister_listeners touches real backbone state; neutralize it.
        self._patch = mock.patch(
            "RNS.Transport.BackboneInterface.deregister_listeners", lambda: None
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for k, v in self._saved.items():
            setattr(Transport, k, v)

    def test_detach_timeout_constant_is_positive_float(self):
        self.assertIsInstance(self._saved["DETACH_TIMEOUT"], float)
        self.assertGreater(self._saved["DETACH_TIMEOUT"], 0)

    def _assert_bounded(self):
        start = time.time()
        Transport.detach_interfaces()
        elapsed = time.time() - start
        # Must return within a small multiple of the budget, NOT hang forever.
        self.assertLess(elapsed, Transport.DETACH_TIMEOUT + 2.0)

    def test_blocking_remote_interface_does_not_hang(self):
        # A non-local interface -> goes to a detach_thread joined with timeout.
        Transport.interfaces = [_blocking_iface()]
        self._assert_bounded()

    def test_blocking_local_client_does_not_hang(self):
        # local_client_interfaces -> detached synchronously in the bounded thread.
        Transport.interfaces = [_blocking_iface(LocalClientInterface)]
        self._assert_bounded()

    def test_blocking_shared_instance_does_not_hang(self):
        # shared instance master -> detached synchronously in the bounded thread.
        Transport.interfaces = [_blocking_iface(LocalServerInterface)]
        self._assert_bounded()

    def test_fast_path_detaches_all_interfaces(self):
        remote = mock.MagicMock(); remote.detached = False
        client = LocalClientInterface.__new__(LocalClientInterface)
        client.detached = False; client.detach = mock.MagicMock()
        Transport.interfaces = [remote, client]
        start = time.time()
        Transport.detach_interfaces()
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)        # no blocking -> quick
        remote.detach.assert_called_once()
        client.detach.assert_called_once()


if __name__ == "__main__":
    unittest.main()
