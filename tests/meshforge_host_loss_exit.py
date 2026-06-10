# MeshForge fork test (mf.5, #69): a wanted-host LocalClientInterface (a
# share_instance=True process that lost the bind race) must exit-to-restart —
# bounded, loud, nonzero — when its host dies AND the abstract socket has no
# listener left, and ONLY when the opt-in env RNS_EXIT_ON_HOST_LOSS=1 is set.
# Every other combination (no env, not wanted-host, too few attempts, listener
# present, listener state UNKNOWN) must keep the stock reconnect-forever
# behaviour. Unknown is not gone: a None listener probe never triggers exit.
import os
import socket
import unittest
from unittest import mock

import RNS  # noqa: F401  (heavy import; mirrors the other tests in this suite)
import RNS.Interfaces.LocalInterface as LI
from RNS.Interfaces.LocalInterface import LocalClientInterface

# Throwaway instance name — never collides with a live rnsd's @rns/default.
INSTANCE = "mf5-fork-test"
ABSTRACT_PATH = "\0rns/" + INSTANCE


def _bare_iface(**attrs):
    # Build a LocalClientInterface without running __init__ (which needs a live
    # RNS.Reticulum singleton); set only what the code under test reads.
    iface = LocalClientInterface.__new__(LocalClientInterface)
    iface.socket_path = ABSTRACT_PATH
    iface.target_ip = None
    iface.target_port = None
    iface.epoll_backend = True  # skip read_loop thread spawn in reconnect()
    iface.online = False
    iface.is_connected_to_shared_instance = True
    iface.never_connected = False
    iface.reconnecting = False
    iface.detached = False
    iface.wanted_host = False
    for k, v in attrs.items():
        setattr(iface, k, v)
    return iface


class HostLossConstantsTest(unittest.TestCase):
    def test_constants(self):
        self.assertIsInstance(LocalClientInterface.HOST_LOSS_EXIT_ATTEMPTS, int)
        self.assertGreater(LocalClientInterface.HOST_LOSS_EXIT_ATTEMPTS, 0)
        self.assertIsInstance(LocalClientInterface.HOST_LOSS_EXIT_CODE, int)
        # Nonzero is load-bearing: rnsd.service has Restart=on-failure.
        self.assertNotEqual(LocalClientInterface.HOST_LOSS_EXIT_CODE, 0)


class ListenerProbeTest(unittest.TestCase):
    def test_present_for_real_bound_listener(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(ABSTRACT_PATH)
            server.listen(1)
            iface = _bare_iface()
            self.assertIs(iface._local_listener_present(), True)
        finally:
            server.close()

    def test_absent_when_nothing_bound(self):
        iface = _bare_iface()
        self.assertIs(iface._local_listener_present(), False)

    def test_none_for_non_abstract_paths(self):
        self.assertIsNone(_bare_iface(socket_path=None)._local_listener_present())
        self.assertIsNone(_bare_iface(socket_path="/tmp/not-abstract")._local_listener_present())

    def test_none_when_proc_unreadable(self):
        iface = _bare_iface()
        with mock.patch("builtins.open", side_effect=OSError("no /proc here")):
            self.assertIsNone(iface._local_listener_present())


class ExitGatingTest(unittest.TestCase):
    # All gates must hold for exit; flipping any single one keeps us alive.
    ATTEMPTS = LocalClientInterface.HOST_LOSS_EXIT_ATTEMPTS

    def _check(self, iface, attempts, env_value):
        env = {"RNS_EXIT_ON_HOST_LOSS": env_value} if env_value is not None else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if env_value is None:
                os.environ.pop("RNS_EXIT_ON_HOST_LOSS", None)
            with mock.patch.object(LI.RNS, "exit") as mock_exit:
                iface._exit_if_host_lost(attempts)
        return mock_exit

    def test_exits_when_all_gates_open(self):
        iface = _bare_iface(wanted_host=True)  # nothing bound -> listener absent
        mock_exit = self._check(iface, self.ATTEMPTS, "1")
        mock_exit.assert_called_once_with(LocalClientInterface.HOST_LOSS_EXIT_CODE)

    def test_no_exit_without_env(self):
        iface = _bare_iface(wanted_host=True)
        self._check(iface, self.ATTEMPTS, None).assert_not_called()

    def test_no_exit_with_env_zero(self):
        iface = _bare_iface(wanted_host=True)
        self._check(iface, self.ATTEMPTS, "0").assert_not_called()

    def test_no_exit_when_not_wanted_host(self):
        iface = _bare_iface(wanted_host=False)
        self._check(iface, self.ATTEMPTS, "1").assert_not_called()

    def test_no_exit_below_attempt_threshold(self):
        iface = _bare_iface(wanted_host=True)
        self._check(iface, self.ATTEMPTS - 1, "1").assert_not_called()

    def test_no_exit_while_listener_present(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(ABSTRACT_PATH)
            server.listen(1)
            iface = _bare_iface(wanted_host=True)
            self._check(iface, self.ATTEMPTS, "1").assert_not_called()
        finally:
            server.close()

    def test_no_exit_when_listener_state_unknown(self):
        # Honest-failure-modes rule: unknown is NOT absence. A None probe
        # (non-abstract transport / unreadable /proc) must never exit.
        iface = _bare_iface(wanted_host=True)
        with mock.patch.object(iface, "_local_listener_present", return_value=None):
            self._check(iface, self.ATTEMPTS, "1").assert_not_called()


class ReconnectLoopIntegrationTest(unittest.TestCase):
    def test_reconnect_loop_exits_after_threshold(self):
        # Host gone (nothing bound at ABSTRACT_PATH), env set, wanted-host:
        # the reconnect loop must invoke RNS.exit after exactly
        # HOST_LOSS_EXIT_ATTEMPTS failed connects — bounded, not infinite.
        iface = _bare_iface(wanted_host=True)
        connect_calls = {"n": 0}

        def failing_connect():
            connect_calls["n"] += 1
            raise ConnectionRefusedError("host is gone")

        with mock.patch.dict(os.environ, {"RNS_EXIT_ON_HOST_LOSS": "1"}):
            with mock.patch.object(LI.time, "sleep"), \
                 mock.patch.object(iface, "connect", side_effect=failing_connect), \
                 mock.patch.object(LI.RNS, "exit", side_effect=SystemExit) as mock_exit:
                with self.assertRaises(SystemExit):
                    iface.reconnect()
        mock_exit.assert_called_once_with(LocalClientInterface.HOST_LOSS_EXIT_CODE)
        self.assertEqual(connect_calls["n"], LocalClientInterface.HOST_LOSS_EXIT_ATTEMPTS)

    def test_reconnect_loop_unbounded_without_env(self):
        # Stock behaviour preserved: without the env, the loop keeps retrying
        # well past the threshold and recovers when a host reappears.
        iface = _bare_iface(wanted_host=True)
        threshold = LocalClientInterface.HOST_LOSS_EXIT_ATTEMPTS
        connect_calls = {"n": 0}

        def eventually_succeeding_connect():
            connect_calls["n"] += 1
            if connect_calls["n"] <= threshold + 2:
                raise ConnectionRefusedError("host is gone")
            iface.online = True

        env_clear = mock.patch.dict(os.environ, {}, clear=False)
        with env_clear:
            os.environ.pop("RNS_EXIT_ON_HOST_LOSS", None)
            with mock.patch.object(LI.time, "sleep"), \
                 mock.patch.object(iface, "connect", side_effect=eventually_succeeding_connect), \
                 mock.patch.object(LI.RNS, "exit") as mock_exit, \
                 mock.patch.object(LI.RNS.Transport, "shared_connection_reappeared"):
                iface.reconnect()
        mock_exit.assert_not_called()
        self.assertEqual(connect_calls["n"], threshold + 3)
        self.assertTrue(iface.online)


if __name__ == "__main__":
    unittest.main()
