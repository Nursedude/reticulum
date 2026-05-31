# MeshForge fork test (#68): LocalClientInterface.connect() must bound the
# shared-instance / local connect with a timeout so a wedged rnsd cannot hang
# the calling thread forever, and must restore the socket to blocking mode for
# the subsequent read/write loop (so on-wire behaviour is unchanged).
import unittest
from unittest import mock

import RNS  # noqa: F401  (heavy import; mirrors the other tests in this suite)
import RNS.Interfaces.LocalInterface as LI
from RNS.Interfaces.LocalInterface import LocalClientInterface


def _bare_iface(**attrs):
    # Build a LocalClientInterface without running __init__ (which needs a live
    # RNS.Reticulum singleton); set only what connect() reads.
    iface = LocalClientInterface.__new__(LocalClientInterface)
    iface.socket_path = None
    iface.target_ip = None
    iface.target_port = None
    iface.epoll_backend = False
    iface.online = False
    iface.is_connected_to_shared_instance = False
    iface.never_connected = True
    for k, v in attrs.items():
        setattr(iface, k, v)
    return iface


class MeshForgeConnectTimeoutTest(unittest.TestCase):
    def test_connect_timeout_constant_is_positive(self):
        self.assertTrue(hasattr(LocalClientInterface, "CONNECT_TIMEOUT"))
        self.assertIsInstance(LocalClientInterface.CONNECT_TIMEOUT, float)
        self.assertGreater(LocalClientInterface.CONNECT_TIMEOUT, 0)

    def test_unix_connect_bracketed_by_timeout_then_blocking(self):
        iface = _bare_iface(socket_path="\0rns/testinstance")
        mock_sock = mock.MagicMock()
        with mock.patch.object(LI.socket, "socket", return_value=mock_sock):
            iface.connect()
        mock_sock.connect.assert_called_once_with("\0rns/testinstance")
        # settimeout(CONNECT_TIMEOUT) BEFORE connect, settimeout(None) AFTER.
        self.assertEqual(
            mock_sock.settimeout.call_args_list,
            [mock.call(LocalClientInterface.CONNECT_TIMEOUT), mock.call(None)],
        )
        self.assertTrue(iface.online)
        self.assertTrue(iface.is_connected_to_shared_instance)

    def test_inet_connect_bracketed_by_timeout_then_blocking(self):
        iface = _bare_iface(socket_path=None, target_ip="127.0.0.1", target_port=37429)
        mock_sock = mock.MagicMock()
        with mock.patch.object(LI.socket, "socket", return_value=mock_sock):
            iface.connect()
        mock_sock.connect.assert_called_once_with(("127.0.0.1", 37429))
        self.assertEqual(
            mock_sock.settimeout.call_args_list,
            [mock.call(LocalClientInterface.CONNECT_TIMEOUT), mock.call(None)],
        )

    def test_wedged_peer_raises_within_bound_not_hang(self):
        # Simulate a wedged peer: connect() blocks until the socket timeout, then
        # raises socket.timeout. Assert connect() propagates it (so reconnect()
        # retries / Reticulum.__init__ falls back) instead of hanging.
        import socket as _socket
        iface = _bare_iface(socket_path="\0rns/wedged")
        mock_sock = mock.MagicMock()
        mock_sock.connect.side_effect = _socket.timeout("timed out")
        with mock.patch.object(LI.socket, "socket", return_value=mock_sock):
            with self.assertRaises(_socket.timeout):
                iface.connect()
        # timeout was armed before the connect attempt
        self.assertIn(
            mock.call(LocalClientInterface.CONNECT_TIMEOUT),
            mock_sock.settimeout.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
