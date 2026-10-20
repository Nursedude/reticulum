# MeshForge fork test (#69 class, sibling of meshforge_require_shared_release):
# a shared instance's normal teardown (Reticulum.exit_handler) must release
# @rns/<instance>/rpc, not only @rns/<instance>.
# Upstream never closed rpc_listener. Since mf.3 the shared-instance listener
# is released on detach, so a process alive after teardown left the RPC name
# bound alone: a new host bound @rns/<instance>, then died constructing its RPC
# Listener with EADDRINUSE. Measured 2026-10-20 on 1.3.8+mf.5 (separate
# process, random name): rc=3 OSError [Errno 98] while the torn-down host
# lived; the same start succeeded once it exited.
#
# Runs as its own process (Reticulum is a singleton). Uses a random instance
# name, so it cannot collide with a real rnsd on the box running it.
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest

import RNS


def _abstract_names(prefix):
    with open("/proc/net/unix") as fh:
        return sorted({l.split()[-1] for l in fh if l.split()[-1].startswith(prefix)})


class MeshForgeRpcReleaseTest(unittest.TestCase):
    def test_teardown_releases_the_rpc_listener(self):
        if not os.path.exists("/proc/net/unix"):
            self.skipTest("needs Linux /proc/net/unix")
        name = "mftest" + secrets.token_hex(4)
        cfgdir = tempfile.mkdtemp(prefix="mf-rpcrelease-")
        self.addCleanup(shutil.rmtree, cfgdir, True)
        with open(os.path.join(cfgdir, "config"), "w") as f:
            f.write(f"[reticulum]\n  share_instance = Yes\n  instance_name = {name}\n"
                    "  enable_transport = No\n[logging]\n  loglevel = 2\n[interfaces]\n")

        self.assertEqual(_abstract_names(f"@rns/{name}"), [], "name already bound")
        r = RNS.Reticulum(configdir=cfgdir)
        self.assertTrue(r.is_shared_instance, "did not come up as the shared instance")
        self.assertIn(f"@rns/{name}/rpc", _abstract_names(f"@rns/{name}"))
        rpc_threads = [t for t in threading.enumerate()
                       if getattr(t, "_target", None) == r.rpc_loop]
        self.assertTrue(rpc_threads, "rpc_loop thread not found — the exit check below would be vacuous")

        # The RPC name must already be gone when detach starts freeing the
        # shared-instance name: released AFTER it, a new host binding the freed
        # name in that gap still dies on EADDRINUSE (code-review medium, F1).
        seen_at_detach = []
        real_detach = RNS.Transport.detach_interfaces
        def recording_detach():
            seen_at_detach.append(_abstract_names(f"@rns/{name}"))
            return real_detach()
        RNS.Transport.detach_interfaces = staticmethod(recording_detach)
        self.addCleanup(setattr, RNS.Transport, "detach_interfaces", staticmethod(real_detach))

        out, err = sys.stdout, sys.stderr          # exit_handler detaches stdio
        try:
            RNS.Reticulum.exit_handler()
        finally:
            sys.stdout, sys.stderr = out, err
        self.assertTrue(seen_at_detach, "detach_interfaces was not called")
        self.assertNotIn(f"@rns/{name}/rpc", seen_at_detach[0],
                         "RPC name still bound when detach began freeing the shared name")

        # The rpc thread may sit in accept(); give the release a moment, but
        # never pass on a name that is still bound.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _abstract_names(f"@rns/{name}"):
            time.sleep(0.05)
        self.assertEqual(_abstract_names(f"@rns/{name}"), [],
                         "torn-down shared instance still holds an @rns name")
        for t in rpc_threads:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "rpc_loop still running after teardown")


if __name__ == "__main__":
    unittest.main()
