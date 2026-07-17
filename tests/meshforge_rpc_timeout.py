# MeshForge fork test (#72): the shared-instance RPC round-trip must be bounded.
# A rnsd that accepts the RPC connection but never answers must not hang the
# calling thread (rnstatus, the in-process map collector, etc.) forever.
import unittest
from unittest import mock

import RNS  # noqa: F401
import RNS.vendor.umsgpack as mp
from RNS.Reticulum import Reticulum


class MeshForgeRpcTimeoutTest(unittest.TestCase):
    def test_rpc_timeout_constant_is_positive(self):
        self.assertIsInstance(Reticulum.RPC_TIMEOUT, float)
        self.assertGreater(Reticulum.RPC_TIMEOUT, 0)

    def test_rpc_recv_returns_response_when_ready(self):
        # 1.3.8 msgpack byte-mode framing: _rpc_recv unpacks recv_bytes().
        conn = mock.MagicMock()
        conn.poll.return_value = True
        conn.recv_bytes.return_value = mp.packb({"ok": 1})
        # self is unused by _rpc_recv; pass None to avoid building a Reticulum.
        result = Reticulum._rpc_recv(None, conn)
        self.assertEqual(result, {"ok": 1})
        conn.poll.assert_called_once_with(Reticulum.RPC_TIMEOUT)
        conn.recv_bytes.assert_called_once()

    def test_rpc_recv_raises_timeout_and_closes_when_no_response(self):
        conn = mock.MagicMock()
        conn.poll.return_value = False  # wedged rnsd: accepted, never answers
        with self.assertRaises(TimeoutError):
            Reticulum._rpc_recv(None, conn)
        conn.recv_bytes.assert_not_called()  # must NOT block on recv_bytes
        conn.close.assert_called_once()  # connection is released


if __name__ == "__main__":
    unittest.main()
