# MeshForge fork test (PipeInterface): a child that stops draining its stdin
# must cost THAT interface, not every other interface on rnsd. Stock
# `process_outgoing` does a blocking `stdin.write()` on the CALLER's thread
# (Transport.transmit <- outbound <- Packet.send), so once the pipe buffer
# fills the sending thread blocks for as long as the child is stalled — the
# 2026-10-10 bench drill measured 47 s with a SIGSTOPped child, the TCP
# interface's traffic going to 0 on the same thread, `online` True
# throughout. Cure: a writer thread + bounded queue; `online` goes False when
# the child has not drained for TX_STALL_S (rnstatus reads Down, callers never
# block), and back to True once it drains again. The child is a real
# subprocess SIGSTOPped/SIGCONTed by the test.
import fcntl
import os
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import RNS
from RNS.Interfaces import PipeInterface as PI

CHILD = textwrap.dedent("""
    import sys
    # drain stdin forever, write nothing back
    while True:
        if not sys.stdin.buffer.read(4096):
            break
""")


class FakeOwner:
    def __init__(self): self.frames = []
    def inbound(self, data, iface): self.frames.append(data)


class MeshForgePipeInterfaceStallTest(unittest.TestCase):
    def setUp(self):
        RNS.loglevel = RNS.LOG_DEBUG
        self.tmp = tempfile.TemporaryDirectory()
        child = os.path.join(self.tmp.name, "child.py")
        with open(child, "w") as f: f.write(CHILD)
        self.cfg = {"name": "stall", "command": f"{sys.executable} -u {child}"}
        self.patches = [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
                        mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=self.tmp.name))),
                        # set by Reticulum.__init__ on a real instance; the respawn path reads it
                        mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]
        for p in self.patches: p.start()
        self.owner = FakeOwner()
        self.iface = PI.PipeInterface(self.owner, self.cfg)
        self.assertTrue(self.iface.online)
        self.child_pid = self.iface.process.pid

    def tearDown(self):
        try: os.kill(self.child_pid, signal.SIGCONT)
        except ProcessLookupError: pass
        try: self.iface.process.kill()
        except Exception: pass
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def _send_many(self, n, size=1000, budget_s=10.0):
        """Send n frames from a worker; return (finished_in_budget, max_call_s)."""
        worst = [0.0]
        def work():
            for _ in range(n):
                t = time.monotonic()
                self.iface.process_outgoing(b"x" * size)
                worst[0] = max(worst[0], time.monotonic() - t)
        th = threading.Thread(target=work, daemon=True); th.start()
        th.join(budget_s)
        return (not th.is_alive(), worst[0])

    def test_healthy_child_drains_and_stays_online(self):
        done, worst = self._send_many(200)
        self.assertTrue(done); self.assertLess(worst, 1.0)
        self.assertTrue(self.iface.online)

    def test_stalled_child_never_blocks_the_caller(self):
        os.kill(self.child_pid, signal.SIGSTOP)
        # Past the kernel pipe capacity, stock blocks the caller here.
        F_GETPIPE_SZ = 1032
        cap = fcntl.fcntl(self.iface.process.stdin.fileno(), F_GETPIPE_SZ)
        done, worst = self._send_many(cap // 1000 + 200, budget_s=8.0)
        self.assertTrue(done, "process_outgoing blocked the caller while the child was stalled")
        self.assertLess(worst, 1.0, f"a single process_outgoing took {worst:.2f}s")

    def _fill_pipe(self):
        """Offer more than the kernel pipe can hold (16 KB-page kernels give
        256 KB, not 64 KB) until the writer is wedged in a write the stopped
        child will never drain. Returns the frames offered."""
        F_GETPIPE_SZ = 1032
        cap = fcntl.fcntl(self.iface.process.stdin.fileno(), F_GETPIPE_SZ)
        offered = 0
        for _ in range(cap // (64 * 1000) + 40):
            self._send_many(64, budget_s=2.0); offered += 64
            time.sleep(0.05)
            if time.monotonic() - self.iface._tx_last_drain > 0.5 and self.iface._txq.full():
                break
        self.assertTrue(self.iface._txq.full(), f"writer never wedged (pipe {cap} B, offered {offered} frames)")
        return offered

    def test_stalled_child_reads_offline_then_recovers(self):
        os.kill(self.child_pid, signal.SIGSTOP)
        self._fill_pipe()
        deadline = time.monotonic() + PI.PipeInterface.TX_STALL_S + 5
        while self.iface.online and time.monotonic() < deadline:
            self.iface.process_outgoing(b"y" * 1000); time.sleep(0.2)
        self.assertFalse(self.iface.online, "online stayed True with a child that drains nothing")
        self.assertGreater(self.iface.tx_dropped, 0)
        os.kill(self.child_pid, signal.SIGCONT)
        deadline = time.monotonic() + 10
        while not self.iface.online and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(self.iface.online, "did not come back online after the child resumed draining")

    def test_child_crash_respawn_stays_online(self):
        """Fresh pass F1 (2026-10-10): after the child died and was respawned,
        the previous writer thread was still parked on the shared queue; it
        took the next frame, broke on the dead child's pipe and set
        online=False beside a LIVE new child, with nothing to set it back."""
        old_pid = self.child_pid
        os.kill(old_pid, signal.SIGKILL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (self.iface.process.pid == old_pid or not self.iface.online):
            time.sleep(0.05)
        self.assertNotEqual(self.iface.process.pid, old_pid, "child was not respawned")
        self.child_pid = self.iface.process.pid
        self.assertTrue(self.iface.online)
        # Offer traffic right after the respawn, the way announces and path
        # requests land on a busy rnsd; far under the queue depth, no stall.
        for _ in range(50):
            self.iface.process_outgoing(b"z" * 200); time.sleep(0.02)
        time.sleep(0.5)
        self.assertTrue(self.iface.online, "online went False after a respawn with a live child")
        self.assertIsNone(self.iface.process.poll(), "respawned child died")
        self.assertEqual(self.iface.tx_dropped, 0)
        writers = [t for t in threading.enumerate() if t.name.startswith("pipe-writer-")]
        self.assertEqual(len(writers), 1, f"writer threads alive: {[t.name for t in writers]}")

    def test_healthy_child_burst_drops_nothing(self):
        """Fresh pass F2: a tight burst of frames a draining child would have
        taken (stock: 0 drops, the kernel pipe absorbs it) must not be dropped
        because the queue filled faster than the writer got the GIL."""
        done, worst = self._send_many(2000, size=1000, budget_s=20.0)
        self.assertTrue(done)
        self.assertLess(worst, 1.0, f"a single process_outgoing took {worst:.2f}s")
        time.sleep(0.5)
        self.assertEqual(self.iface.tx_dropped, 0, f"{self.iface.tx_dropped} frames dropped with a healthy child")
        self.assertTrue(self.iface.online)


if __name__ == "__main__":
    unittest.main()
