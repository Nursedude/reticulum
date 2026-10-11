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
    import os, sys, time
    # drain stdin forever, write nothing back; argv "slow" = drain 200 B per
    # 100 ms (a LoRa-rate consumer that is alive but behind)
    slow = "slow" in sys.argv[1:]
    rate = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "rate" else None
    fd = sys.stdin.fileno()
    while True:
        if rate:
            d = os.read(fd, max(200, int(rate * 0.02)))
            if not d: break
            time.sleep(len(d) / rate)
        elif slow:
            if not os.read(fd, 200): break
            time.sleep(0.1)
        elif not os.read(fd, 4096):
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

    def _swap_iface(self, cfg):
        """Replace the fixture's interface with one built from cfg; retire
        BOTH at cleanup (writer threads exit only on detach or a generation
        change, and test_child_crash_respawn_stays_online counts them)."""
        def retire(i):
            i.detached = True
            try: i.process.kill()
            except Exception: pass
            if i._writer is not None: i._writer.join(2)
        orig = self.iface
        new = PI.PipeInterface(self.owner, cfg)
        self.addCleanup(retire, new)
        self.addCleanup(retire, orig)
        self.iface = new
        return new

    def _spy_congestion(self):
        """Record every entry into congested mode by the code itself — a
        polling watcher thread is starved by the very contention under test
        (fresh pass on a67be362) and undercounts."""
        entered = []
        class Spy(type(self.iface)):
            @property
            def _tx_congested(self): return self.__dict__.get("_spy_c", False)
            @_tx_congested.setter
            def _tx_congested(self, v):
                if v and not self.__dict__.get("_spy_c", False): entered.append(time.monotonic())
                self.__dict__["_spy_c"] = v
        self.iface.__class__ = Spy
        return entered

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


    def test_slow_child_costs_a_caller_one_wait_per_congestion_episode(self):
        """Transport-forwarding drill 2026-10-10: with `enable_transport = Yes`
        a forwarded frame is transmitted on the reader thread of the interface
        it ARRIVED on. A child that drains, but slower than it is offered,
        kept `_tx_last_drain` fresh so the stall check never fired, and every
        frame past its drain rate paid TX_PUT_WAIT_S on that reader: 100 f/s
        x 20 ms saturated it, and control traffic on the arriving TCP
        interface (never touching the pipe) ran 8 s median / 20 s worst.
        Once a put has timed out with the kernel pipe FULL (the child is
        behind, not the writer unscheduled), the next frames must not wait
        again until the queue has drained to TX_LOW_WATER."""
        self._swap_iface({"name": "slow", "command": self.cfg["command"] + " slow"})
        # Isolate the caller cost from the stall detector: never go offline here.
        with mock.patch.object(PI.PipeInterface, "TX_STALL_S", 3600.0):
            deadline = time.monotonic() + 20
            while self.iface.tx_dropped == 0 and time.monotonic() < deadline:
                self.iface.process_outgoing(b"c" * 1000)
            self.assertGreater(self.iface.tx_dropped, 0, "queue never filled behind the slow child")
            slow_calls = 0; total = 0.0
            for _ in range(100):
                t = time.monotonic(); self.iface.process_outgoing(b"c" * 1000); dt = time.monotonic() - t
                total += dt
                if dt >= 0.015: slow_calls += 1
            self.assertTrue(self.iface.online, "went offline; the measurement would be vacuous")
        self.assertLessEqual(slow_calls, 2, f"{slow_calls} of 100 calls waited for a slot ({total:.2f}s on the caller)")

    def test_stall_recovery_waits_for_the_queue_to_drain_to_low_water(self):
        """Same drill: a child draining 2 kB/s on a 16 KB-page kernel frees a
        pipe page every ~8 s > TX_STALL_S, so the interface flapped Down/Up
        every ~8 s (the first completed write after a stall flipped it back
        with the queue still full). Recovery must wait until the queue has
        drained to TX_LOW_WATER."""
        at_flip = []
        real_log = RNS.log
        def spy(msg, *a, **k):
            if "draining again" in str(msg): at_flip.append(self.iface._txq.qsize())
            return real_log(msg, *a, **k)
        os.kill(self.child_pid, signal.SIGSTOP)
        self._fill_pipe()
        deadline = time.monotonic() + PI.PipeInterface.TX_STALL_S + 5
        while self.iface.online and time.monotonic() < deadline:
            self.iface.process_outgoing(b"y" * 1000); time.sleep(0.2)
        self.assertFalse(self.iface.online)
        with mock.patch.object(RNS, "log", spy):
            os.kill(self.child_pid, signal.SIGCONT)
            deadline = time.monotonic() + 10
            while not self.iface.online and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertTrue(self.iface.online)
        self.assertEqual(len(at_flip), 1, at_flip)
        self.assertLessEqual(at_flip[0], PI.PipeInterface.TX_QUEUE_DEPTH // 2,
                             f"came back online with {at_flip[0]} frames still queued")


    def _contend(self, senders, burners, hogs, frames=500):
        """Healthy (fast-draining) child; `senders` threads offer `frames`
        each while `burners` run bytecode loops and `hogs` make long C calls
        that hold the GIL (~75 ms each on a Pi 5)."""
        entered = self._spy_congestion()
        stop = threading.Event()
        def burn():
            x = 0
            while not stop.is_set():
                for _ in range(100000): x += 1
        def hog():
            while not stop.is_set(): sum(range(3000000))
        def send():
            for _ in range(frames): self.iface.process_outgoing(b"x" * 1000)
        aux = [threading.Thread(target=burn, daemon=True) for _ in range(burners)] + \
              [threading.Thread(target=hog, daemon=True) for _ in range(hogs)]
        for t in aux: t.start()
        ss = [threading.Thread(target=send, daemon=True) for _ in range(senders)]
        try:
            for t in ss: t.start()
            for t in ss: t.join(90)
        finally:
            stop.set()
        self.assertFalse(any(t.is_alive() for t in ss), "senders did not finish")
        return entered

    def test_healthy_child_under_gil_contention_keeps_back_pressure(self):
        """Fresh pass on 94cd084d, F1: one 20 ms put timeout caused by the
        WRITER being starved of the GIL (child healthy) entered congested
        mode, and every later frame met a full queue with no wait: 4 tight
        senders + 1 CPU-burning thread dropped 1242-1739 of 2000 where
        1.3.8+mf.6 dropped 67-117. Congestion is for a child that is behind,
        not for a writer that has not been scheduled."""
        entered = self._contend(senders=4, burners=1, hogs=0)
        self.assertEqual(entered, [], "entered congested mode with a child that was draining")
        self.assertLess(self.iface.tx_dropped, 500, f"{self.iface.tx_dropped} of 2000 dropped with a healthy child")
        self.assertTrue(self.iface.online)

    def test_healthy_child_under_long_gil_holds_never_congests(self):
        """Fresh pass on a67be362, F2: a clock-based gate (writer inside a
        write >= 0.1 s) also counts the GIL re-acquire after the syscall, so
        long C calls on other threads (4 senders + 1 burner + 2 GIL hogs)
        still entered congestion with a healthy child: 1739 of 2000 dropped
        vs 207 on 1.3.8+mf.6, with the pipe WRITABLE when sampled. The
        evidence must be the kernel's: a full pipe."""
        entered = self._contend(senders=4, burners=1, hogs=2, frames=250)
        self.assertEqual(entered, [], "entered congested mode with a child that was draining")
        self.assertTrue(self.iface.online)

    def test_child_behind_at_a_fast_rate_still_congests(self):
        """Fresh pass on a67be362, F1: the kernel wakes a pipe writer only
        when a whole PAGE has been consumed, so a child draining 200 kB/s
        (16 KB pages: a block every ~80 ms) never blocked one write for
        0.1 s; offered 300 kB/s it was BEHIND (this test offers 600 kB/s so
        the 256 KB pipe + 256-frame queue fill within its 5 s), never congested, and every
        frame paid TX_PUT_WAIT_S on the caller again (496 waits in 15 s,
        caller busy 100% — the mf.6 reader-saturation shape). On a 4 KB-page
        kernel that band starts near 41 kB/s."""
        self._swap_iface({"name": "fast", "command": self.cfg["command"] + " rate 200000"})
        entered = self._spy_congestion()
        waits = 0; busy = 0.0
        t_end = time.monotonic() + 5.0; k = 0; t0 = time.monotonic()
        while time.monotonic() < t_end:
            k += 1
            t = time.monotonic(); self.iface.process_outgoing(b"f" * 1000); dt = time.monotonic() - t
            busy += dt
            if dt >= 0.015: waits += 1
            nxt = t0 + k / 600.0
            if nxt > time.monotonic(): time.sleep(nxt - time.monotonic())
        self.assertGreater(self.iface.tx_dropped, 0, "the child was never behind; the shape is vacuous")
        self.assertNotEqual(entered, [], "a child that was behind never entered congested mode")
        self.assertLessEqual(waits, 5, f"{waits} calls waited for a slot ({busy:.2f}s of 5s on the caller)")


if __name__ == "__main__":
    unittest.main()
