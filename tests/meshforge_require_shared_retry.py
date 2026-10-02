# MeshForge fork test (#69 class, mf.4): after Reticulum(require_shared_instance=True)
# refuses because no shared instance is running, the SAME process can retry
# once a host appears, and joins it as a client. Upstream left the singleton
# latched, so the retry raised OSError("Attempt to reinitialise Reticulum").
# Runs a throwaway host in a child process on a random instance name, so it
# cannot touch a real rnsd on the box.
import multiprocessing
import os
import secrets
import shutil
import tempfile
import time
import unittest

import RNS


def _cfg(d, name):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config"), "w") as f:
        f.write(f"[reticulum]\n  share_instance = Yes\n  instance_name = {name}\n"
                "  enable_transport = No\n[logging]\n  loglevel = 2\n[interfaces]\n")


def _host(d, ready, stop):
    r = RNS.Reticulum(configdir=d)
    ready.set() if r.is_shared_instance else None
    stop.wait(30)
    os._exit(0)


class MeshForgeRequireSharedRetryTest(unittest.TestCase):
    def test_refusal_then_retry_joins_the_host(self):
        if not os.path.exists("/proc/net/unix"):
            self.skipTest("needs Linux abstract sockets")
        name = "mfretry" + secrets.token_hex(4)
        base = tempfile.mkdtemp(prefix="mf-retry-")
        self.addCleanup(shutil.rmtree, base, True)
        _cfg(os.path.join(base, "client"), name)
        _cfg(os.path.join(base, "host"), name)

        with self.assertRaises(SystemError):
            RNS.Reticulum(configdir=os.path.join(base, "client"), require_shared_instance=True)
        self.assertIsNone(RNS.Reticulum.get_instance(), "refusal left a half-built singleton")
        # Callers feature-detect the fix by this marker (stock RNS lacks both).
        self.assertTrue(getattr(RNS.Reticulum, "MF_REQUIRE_SHARED_RETRYABLE", False))

        ctx = multiprocessing.get_context("fork")
        ready, stop = ctx.Event(), ctx.Event()
        host = ctx.Process(target=_host, args=(os.path.join(base, "host"), ready, stop), daemon=True)
        host.start()
        self.addCleanup(lambda: (stop.set(), host.join(5)))
        self.assertTrue(ready.wait(20), "throwaway host never became the shared instance")

        r = RNS.Reticulum(configdir=os.path.join(base, "client"), require_shared_instance=True)
        self.assertFalse(r.is_shared_instance)
        self.assertTrue(r.is_connected_to_shared_instance)


if __name__ == "__main__":
    unittest.main()
