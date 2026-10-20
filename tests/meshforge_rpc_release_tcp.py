# MeshForge fork test: the TCP twin of meshforge_rpc_release.py. With
# shared_instance_type = tcp the RPC listener is 127.0.0.1:<control port>;
# teardown must free that port. Its own module because Reticulum is a
# singleton: one shared instance per process.
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import RNS


class MeshForgeRpcReleaseTcpTest(unittest.TestCase):
    """shared_instance_type = tcp: the RPC listener is 127.0.0.1:<control
    port>. After teardown that port must be bindable again (non-author
    review of the AF_UNIX fix, 2026-10-20)."""

    def test_teardown_frees_the_rpc_control_port(self):
        import socket
        # Both sockets held open while picking, so the kernel cannot hand the
        # same ephemeral port back twice (code-review medium, F5).
        with socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0)); b.bind(("127.0.0.1", 0))
            data_port, ctrl_port = a.getsockname()[1], b.getsockname()[1]
        cfgdir = tempfile.mkdtemp(prefix="mf-rpcrelease-tcp-")
        self.addCleanup(shutil.rmtree, cfgdir, True)
        with open(os.path.join(cfgdir, "config"), "w") as f:
            f.write("[reticulum]\n  share_instance = Yes\n  shared_instance_type = tcp\n"
                    f"  shared_instance_port = {data_port}\n  instance_control_port = {ctrl_port}\n"
                    "  enable_transport = No\n[logging]\n  loglevel = 2\n[interfaces]\n")
        r = RNS.Reticulum(configdir=cfgdir)
        self.assertTrue(r.is_shared_instance, "did not come up as the shared instance")
        rpc_threads = [t for t in threading.enumerate()
                       if getattr(t, "_target", None) == r.rpc_loop]
        self.assertTrue(rpc_threads, "rpc_loop thread not found")
        out, err = sys.stdout, sys.stderr
        try:
            RNS.Reticulum.exit_handler()
        finally:
            sys.stdout, sys.stderr = out, err
        deadline, last = time.monotonic() + 3.0, None
        while time.monotonic() < deadline:
            try:
                with socket.socket() as s:
                    s.bind(("127.0.0.1", ctrl_port))
                for t in rpc_threads:                   # code-review medium, F7
                    t.join(timeout=3.0)
                    self.assertFalse(t.is_alive(), "rpc_loop still running after teardown")
                return
            except OSError as e:
                last = e
                time.sleep(0.05)
        self.fail(f"RPC control port {ctrl_port} still bound after teardown: {last}")


if __name__ == "__main__":
    unittest.main()
