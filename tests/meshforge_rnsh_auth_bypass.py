# MeshForge fork test (1.3.9 port): the rnsh listener authentication bypass.
#
# In 1.3.8, ListenerSession._initiator_identified() called
# terminate("Identity is not allowed.") for an identity outside the allow list
# and then FELL THROUGH to _set_state(LSSTATE_WAIT_VERS), overwriting the
# ERROR state terminate() had just set. The refused session stayed usable, and
# _start_cmd() had no authentication check of its own: an unauthorized
# identity could get the listener's command spawned (remote command
# execution). Upstream 1.3.9 fixed it; the port carries that fix.
#
# Measured 2026-10-26, in-process (no link, no network, the spawn replaced by a
# recorder), driving identify -> VersionInfo -> ExecuteCommand: on 1.3.8+mf.11
# the intruder reaches the spawn with ['/bin/true']; on stock 1.3.9 and
# 1.3.9+mf.0 nothing is spawned. The allowed-identity case is the control: it
# must still reach the spawn on every tree, so a test that refuses everyone
# cannot pass.
import asyncio
import unittest
from unittest import mock

import RNS
from RNS.Utilities.rnsh import session as S


class _Recorder:
    spawned = []

    def __init__(self, *a, **kw):
        _Recorder.spawned.append(kw.get("argv"))
        self.running = True

    def start(self):
        pass

    def __getattr__(self, name):
        return mock.MagicMock()


class MeshForgeRnshAuthBypassTest(unittest.TestCase):
    def setUp(self):
        self.allowed, self.intruder = RNS.Identity(), RNS.Identity()
        self._saved = {k: getattr(S.ListenerSession, k) for k in (
            "allowed_identity_hashes", "allow_all", "allow_remote_command", "default_command")}
        S.ListenerSession.allowed_identity_hashes = [self.allowed.hash]
        S.ListenerSession.allow_all = False
        S.ListenerSession.allow_remote_command = False
        S.ListenerSession.default_command = ["/bin/true"]
        _Recorder.spawned = []
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(S.ListenerSession, k, v)
        S.ListenerSession.sessions.clear()
        self.loop.close()

    def _identify_then_start(self, identity):
        # The wire path an initiator drives: identify, version, execute. Calling
        # _start_cmd() directly is NOT enough: 1.3.9 also refuses a spawn outside
        # LSSTATE_RUNNING, so a direct call is refused for the allowed identity
        # too and the test could not tell auth from state.
        outlet = mock.MagicMock()
        outlet.rtt = 0.1
        sess = S.ListenerSession(outlet, mock.MagicMock(), self.loop)
        sess._initiator_identified(outlet, identity)
        with mock.patch.object(S.process, "CallbackSubprocess", _Recorder):
            sess._handle_message(S.protocol.VersionInfoMessage())
            sess._handle_message(S.protocol.ExecuteCommandMesssage(
                cmdline=[], term="xterm", rows=24, cols=80, hpix=0, vpix=0))
        return sess

    def test_unauthorized_identity_never_reaches_spawn(self):
        sess = self._identify_then_start(self.intruder)
        self.assertEqual(_Recorder.spawned, [])
        self.assertNotEqual(sess.state, S.LSState.LSSTATE_RUNNING)

    def test_control_allowed_identity_still_spawns(self):
        sess = self._identify_then_start(self.allowed)
        self.assertEqual(_Recorder.spawned, [["/bin/true"]])
        self.assertEqual(sess.remote_identity, self.allowed)


if __name__ == "__main__":
    unittest.main()
