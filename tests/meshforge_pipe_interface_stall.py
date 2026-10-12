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
#
# Frame size (fresh pass on b18aae01, F1): offer 500-B payloads by default —
# the RNS MTU, and what the fleet actually sends — with 1000 B as the second
# point. Pipe pages pack whole writes, so a rule can be right at 1000 B by
# coincidence (16 frames fill a 16 KB page exactly) and wrong at 500 B; a
# shape tested at one size pins that size, not the rule.
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
    # "gulp R": read up to 1 MB at once, then sleep for it at R bytes/s — the
    # same mean rate as "rate R" but it empties the whole pipe in one read
    # (a child that os.read()s a large buffer), fresh pass on 925d275c F1.
    gulp = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "gulp" else None
    fd = sys.stdin.fileno()
    while True:
        if gulp:
            d = os.read(fd, 1 << 20)
            if not d: break
            time.sleep(len(d) / gulp)
        elif rate:
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
        # Detach BEFORE killing: otherwise the read loop RESPAWNS the killed
        # child and its new writer thread and process outlive the test (seen
        # when a fixture-only test first preceded the respawn test, which
        # counts writer threads).
        self.iface.detached = True
        try: self.iface.process.kill()
        except Exception: pass
        if getattr(self.iface, "_writer", None) is not None: self.iface._writer.join(2)
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
        Once puts have timed out with the kernel pipe FULL for
        TX_CONGEST_FULL_S (the child is behind — not the writer unscheduled,
        not the child unscheduled for one slice), the next frames must not
        wait again until the queue has drained to TX_LOW_WATER. The budget
        is the gate's own cost: one caller pays one wait per TX_PUT_WAIT_S of
        the span (3 more after the first, measured 3 of 100) — never 100."""
        # getattr: on a tree without the gate this must FAIL on the waits, not
        # ERROR on the name (fresh pass on 925d275c, F4).
        allowed = int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 1
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
        self.assertLessEqual(slow_calls, allowed, f"{slow_calls} of 100 calls waited for a slot ({total:.2f}s on the caller; the gate allows {allowed})")

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
            # One clock read: two reads let the deadline pass in between and
            # hand sleep() a negative length (fresh pass on 6adfa33b, F2).
            rem = nxt - time.monotonic()
            if rem > 0: time.sleep(rem)
        self.assertGreater(self.iface.tx_dropped, 0, "the child was never behind; the shape is vacuous")
        self.assertNotEqual(entered, [], "a child that was behind never entered congested mode")
        # The gate's own cost, once: TX_CONGEST_FULL_S of waits before the
        # episode opens (one caller: ~25), then none until low water.
        allowed = int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 6
        self.assertLessEqual(waits, allowed, f"{waits} calls waited for a slot ({busy:.2f}s of 5s on the caller; the gate allows {allowed})")

    def test_child_behind_faster_than_a_page_per_put_wait_still_congests(self):
        """Fresh pass on b18aae01, F2: a child behind at a rate that frees a
        pipe page within TX_PUT_WAIT_S (16 KB / 20 ms = 800 kB/s on 16 KB
        pages; ~200 kB/s on 4 KB pages) never lets a put TIME OUT — every put
        waits up to 20 ms and then succeeds — so the pipe was never sampled,
        no span opened, and the caller paid a wait on nearly every frame
        exactly as on mf.6 (reviewer: 0 episodes, 685 waits, reader duty
        0.917). A put that waited for its slot and found the pipe full is the
        same evidence as one that timed out. 500 B at 2400 f/s (the RNS MTU)
        and 1000 B at 1200 f/s: both 1.2 MB/s into an 800 kB/s child."""
        for size, rate_fs in ((500, 2400.0), (1000, 1200.0)):
            with self.subTest(size=size):
                self._assert_fast_child_congests(size, rate_fs)

    def _assert_fast_child_congests(self, size, rate_fs):
        self._swap_iface({"name": f"fast{size}", "command": self.cfg["command"] + " rate 800000"})
        entered = self._spy_congestion()
        waits = 0; busy = 0.0
        t_end = time.monotonic() + 5.0; k = 0; t0 = time.monotonic()
        while time.monotonic() < t_end:
            k += 1
            t = time.monotonic(); self.iface.process_outgoing(b"f" * size); dt = time.monotonic() - t
            busy += dt
            if dt >= 0.015: waits += 1
            rem = t0 + k / rate_fs - time.monotonic()
            if rem > 0: time.sleep(rem)
        # In this band puts SUCCEED after waiting, so tx_dropped may stay 0 on
        # a tree that misses it: the caller's waits are the vacuity witness.
        self.assertGreater(waits + self.iface.tx_dropped, 0, f"{size}-B frames: the caller never waited and nothing dropped; the child was never behind and the shape is vacuous")
        self.assertNotEqual(entered, [], f"{size}-B frames: a child behind at 800 kB/s never entered congested mode ({waits} waits, {busy:.2f}s of 5s on the caller, {self.iface.tx_dropped} dropped)")
        allowed = int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 6
        self.assertLessEqual(waits, allowed, f"{size}-B frames: {waits} calls waited for a slot ({busy:.2f}s of 5s on the caller; the gate allows {allowed})")

    def test_starved_writer_with_a_draining_child_never_congests(self):
        """The kernel-fact leg of the gate, pinned on its own: a queue that
        stays full for longer than TX_CONGEST_FULL_S because the WRITER is
        slow (GIL-starved) while the child drains everything it gets (pipe
        writable at every sample) is not a child behind. Fresh pass on
        94cd084d, F1, made deterministic: the writer's stdin.write is slowed
        to 30 ms per frame (slower than TX_PUT_WAIT_S, so puts time out); 4
        tight senders keep the queue full for ~3 s. With the pipe never full
        the interface must keep mf.6 back-pressure."""
        entered = self._spy_congestion()
        real = self.iface.process.stdin
        class SlowStdin:
            def write(self, d): time.sleep(0.03); return real.write(d)
            def flush(self): return real.flush()
            def fileno(self): return real.fileno()
            def close(self): return real.close()
        self.iface.process.stdin = SlowStdin()
        def send():
            for _ in range(150): self.iface.process_outgoing(b"w" * 1000)
        ss = [threading.Thread(target=send, daemon=True) for _ in range(4)]
        for t in ss: t.start()
        for t in ss: t.join(30)
        self.assertFalse(any(t.is_alive() for t in ss), "senders did not finish")
        self.assertGreater(self.iface.tx_dropped, 0, "no put timed out; the shape is vacuous")
        self.assertGreater(self.iface.txb, 0)
        self.assertEqual(entered, [], f"entered congested mode with the pipe writable ({self.iface.tx_dropped} of 600 dropped)")
        self.assertTrue(self.iface.online)

    def test_healthy_child_paused_briefly_never_congests(self):
        """Fresh pass on 6adfa33b, F1: "pipe full" is not "child behind". A
        HEALTHY child that is merely unscheduled longer than TX_PUT_WAIT_S
        (a `nice 10` child beside 12 busy processes) under a burst filled the
        pipe + queue, the first timed-out put found the pipe full, and the
        interface congested: 1487 of 2000 dropped (three interleaved runs,
        each exactly pipe + queue then everything) where 1.3.8+mf.6 dropped
        0-93. Here the pause is explicit: the child is SIGSTOPped until the
        first put times out (pipe full by construction), then resumed at
        once — a scheduler hiccup, not a child that is behind. The pipe must
        stay full for TX_CONGEST_FULL_S of timed-out puts before congestion."""
        entered = self._spy_congestion()
        stop = threading.Event(); offered = [0]
        def send():
            while not stop.is_set():
                self.iface.process_outgoing(b"p" * 1000); offered[0] += 1
        ss = [threading.Thread(target=send, daemon=True) for _ in range(4)]
        for t in ss: t.start()
        try:
            for cycle in range(5):
                dropped0 = self.iface.tx_dropped
                os.kill(self.child_pid, signal.SIGSTOP)
                deadline = time.monotonic() + 5
                while self.iface.tx_dropped == dropped0 and time.monotonic() < deadline:
                    time.sleep(0.001)
                os.kill(self.child_pid, signal.SIGCONT)
                self.assertGreater(self.iface.tx_dropped, dropped0, f"cycle {cycle}: no put timed out; the shape is vacuous")
                # Let the resumed child drain pipe + queue well past the gate
                # window before the next pause, so each pause is its own event.
                time.sleep(0.3)
        finally:
            stop.set()
            for t in ss: t.join(10)
        self.assertFalse(any(t.is_alive() for t in ss), "senders did not finish")
        self.assertEqual(entered, [], f"entered congested mode {len(entered)}x with a child that was only paused briefly ({offered[0]} offered, {self.iface.tx_dropped} dropped)")
        self.assertTrue(self.iface.online)

    def test_short_burst_into_a_slower_child_is_delivered_not_dropped(self):
        """Fresh pass on 6adfa33b, F1, the shape as the reviewer ran it: a
        `nice 10` child beside 12 busy processes drains a 2 MB burst in 0.3 s
        (6.9 MB/s — slower than the senders, behind for the burst's length)
        and on mf.6 delivered all of it for 19–44 waits; a gate that calls
        the child behind after one full sample — or after 60 ms of them —
        dropped 1224–1487 of 2000, because once congested nothing throttles
        the senders. Deterministic form: a child draining 2 MB/s offered a
        1 MB burst tight from 4 senders; buffers hold 512 KB, the rest is
        0.25 s of a full pipe. A burst shorter than TX_CONGEST_FULL_S is
        back-pressured and delivered (one drop per timed-out put, as mf.6),
        never dropped wholesale."""
        self._swap_iface({"name": "burst", "command": self.cfg["command"] + " rate 2000000"})
        entered = self._spy_congestion()
        def send():
            for _ in range(250): self.iface.process_outgoing(b"b" * 1000)
        ss = [threading.Thread(target=send, daemon=True) for _ in range(4)]
        for t in ss: t.start()
        for t in ss: t.join(20)
        self.assertFalse(any(t.is_alive() for t in ss), "senders did not finish")
        self.assertEqual(entered, [], f"entered congested mode on a {1000 * 1000 // 1000} kB burst ({self.iface.tx_dropped} of 1000 dropped)")
        self.assertLessEqual(self.iface.tx_dropped, 150, f"{self.iface.tx_dropped} of 1000 dropped: the burst was dropped, not back-pressured")
        self.assertTrue(self.iface.online)

    def test_congestion_episode_is_logged_at_entry_and_exit(self):
        """Fresh pass on 6adfa33b, F3: `tx_dropped` had no reader — not in the
        interface stats, not in rnstatus — and the only log lines were the two
        stall WARNINGs, so a congestion episode with a child that keeps
        draining (the whole band this cure is for: 1017 frames dropped in 15 s
        at 200 kB/s drained / 300 kB/s offered) left NOTHING an operator could
        see while rnstatus read Up. Every episode now leaves a witness: one
        line when it begins, one with the count when the child has caught up."""
        self._swap_iface({"name": "fast", "command": self.cfg["command"] + " rate 200000"})
        lines = []
        real_log = RNS.log
        def spy(msg, *a, **k):
            if str(self.iface) in str(msg): lines.append(str(msg))
            return real_log(msg, *a, **k)
        with mock.patch.object(RNS, "log", spy):
            t_end = time.monotonic() + 3.0; k = 0; t0 = time.monotonic()
            while time.monotonic() < t_end:
                k += 1; self.iface.process_outgoing(b"f" * 1000)
                rem = t0 + k / 600.0 - time.monotonic()
                if rem > 0: time.sleep(rem)
            self.assertTrue(getattr(self.iface, "tx_congested", False), "the child was never behind; the shape is vacuous")
            entry = [l for l in lines if "behind" in l]
            self.assertEqual(len(entry), 1, lines)
            # Stop offering: 256 queued frames drain at 200 f/s in ~1.3 s. The
            # episode closes only after the child has stayed caught up across
            # TX_CONGEST_FULL_S of offers (fresh pass on b18aae01, F1), so
            # offer slowly for twice that: 20 frames at 50 ms, 20 kB/s.
            time.sleep(2.0)
            for _ in range(int(2 * PI.PipeInterface.TX_CONGEST_FULL_S / 0.05)):
                self.iface.process_outgoing(b"f" * 1000)
                time.sleep(0.05)
            self.assertFalse(getattr(self.iface, "tx_congested", False))
            exit_ = [l for l in lines if "caught up" in l]
            self.assertEqual(len(exit_), 1, lines)
            self.assertIn("dropped during the episode", exit_[0])
        self.assertTrue(self.iface.online)

    def test_offers_while_offline_after_a_crash_are_counted_and_the_episode_closes(self):
        """Fresh pass on b18aae01, F3 (pre-existing since mf.6): offers made
        while `online` is False for any reason but a stall — a dead child, the
        respawn window — were discarded with no counter (1363 frames in the
        reviewer's respawn drill), and a respawn ended an open congestion
        episode silently (2 of 3 episodes had no closing line). Every refused
        offer is a drop and counts; an episode a respawn ends leaves a line
        with its count, like one the child ends by catching up."""
        self._swap_iface({"name": "crash", "command": self.cfg["command"] + " rate 200000", "respawn_delay": "1"})
        lines = []
        real_log = RNS.log
        def spy(msg, *a, **k):
            if str(self.iface) in str(msg): lines.append(str(msg))
            return real_log(msg, *a, **k)
        with mock.patch.object(RNS, "log", spy):
            t_end = time.monotonic() + 3.0; k = 0; t0 = time.monotonic()
            while time.monotonic() < t_end and not getattr(self.iface, "tx_congested", False):
                k += 1; self.iface.process_outgoing(b"c" * 500)
                rem = t0 + k / 1200.0 - time.monotonic()
                if rem > 0: time.sleep(rem)
            self.assertTrue(getattr(self.iface, "tx_congested", False), "the child was never behind; the shape is vacuous")
            old_pid = self.iface.process.pid
            os.kill(old_pid, signal.SIGKILL)
            # Offer at 50 f/s through the crash and the respawn window; count
            # only offers made with `online` False both before and after the
            # call, so a respawn landing mid-call cannot be miscounted.
            d0 = self.iface.tx_dropped; offline = 0
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and (self.iface.process.pid == old_pid or not self.iface.online):
                before = self.iface.online
                self.iface.process_outgoing(b"c" * 500)
                if not before and not self.iface.online: offline += 1
                time.sleep(0.02)
            self.assertNotEqual(self.iface.process.pid, old_pid, "child was not respawned")
            self.assertGreater(offline, 0, "no offer landed while offline; the shape is vacuous")
            self.assertGreaterEqual(self.iface.tx_dropped - d0, offline,
                                    f"{offline} offers were refused while offline but tx_dropped grew by {self.iface.tx_dropped - d0}")
            self.assertFalse(getattr(self.iface, "tx_congested", False), "a respawned child inherited the dead child's episode")
            closed = [l for l in lines if "dropped during the episode" in l]
            self.assertEqual(len(closed), 1, f"the episode the crash ended left no closing line: {lines}")

    def test_child_behind_that_reads_in_gulps_congests_once(self):
        """Fresh pass on 925d275c, F1: "the queue drained to low water" was
        read as "the child caught up", but a child that is behind and reads
        in GULPS (os.read of a large buffer: one read empties the 256 KB pipe)
        makes it true every cycle — the writer dumps the whole queue into the
        emptied pipe, the queue reads empty, the episode closes, the span
        resets, and the next TX_CONGEST_FULL_S of timed-out puts is paid again
        on the reader: 9 episodes in 30 s, reader duty 0.31 vs 0.02 on
        6adfa33b, "caught up" logged nine times about a child that never did.
        Caught up means NOTHING is backed up: the queue is empty AND the kernel
        pipe has room. A child behind at a steady rate congests once. (1000-B
        frames, 600 f/s — the shape that was green on b18aae01 by coincidence.)"""
        self._assert_gulp_child_congests_once(1000, 600.0)

    def test_child_behind_that_reads_in_gulps_congests_once_at_the_rns_mtu(self):
        """Fresh pass on b18aae01, F1: "queue empty AND pipe writable" is a
        single sample, and at the RNS MTU it is true while the child is 1.3 s
        behind — 502-B frames pack 32 to a 16 KB page, so the 256-frame queue
        dump fills 8 of the pipe's 16 pages and select() reads writable. The
        episode closed, the span re-armed, and the reader paid it again every
        ~1.3 s (drill: 14 episodes / 368 waits / duty 0.38 in 20 s vs 6adfa33b
        16 waits). The 1000-B twin above passed because 16 frames fill a page
        exactly. Caught up must PERSIST, as the entry evidence must."""
        self._assert_gulp_child_congests_once(500, 1200.0)

    def _assert_gulp_child_congests_once(self, size, rate_fs):
        self._swap_iface({"name": "gulp", "command": self.cfg["command"] + " gulp 200000"})
        entered = self._spy_congestion()
        waits = 0; busy = 0.0
        t_end = time.monotonic() + 6.0; k = 0; t0 = time.monotonic()
        while time.monotonic() < t_end:
            k += 1
            t = time.monotonic(); self.iface.process_outgoing(b"g" * size); dt = time.monotonic() - t
            busy += dt
            if dt >= 0.015: waits += 1
            rem = t0 + k / rate_fs - time.monotonic()
            if rem > 0: time.sleep(rem)
        self.assertGreater(self.iface.tx_dropped, 0, "the child was never behind; the shape is vacuous")
        self.assertNotEqual(entered, [], "a child that was behind never entered congested mode")
        self.assertEqual(len(entered), 1, f"{size}-B frames: congested {len(entered)}x in 6 s: the gate re-armed on a child that never caught up ({waits} waits, {busy:.2f}s on the caller)")
        # Up to TWO spans of waits, once: a gulp that lands inside the first
        # span empties the pipe, the next sample reads writable and the span
        # restarts — correctly, the child was not behind at that instant. The
        # partial span is lost (measured at 500 B: waits 1.06-1.24 s, gulp,
        # span restarts 1.49 s, entry 2.01 s = 36 waits with or without the
        # waited-put sampling). A re-arming gate costs a span per gulp cycle
        # (b18aae01: 91-104 waits in 6 s), far above this bound, and
        # `entered == 1` above is what pins the re-arm.
        allowed = 2 * int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 6
        self.assertLessEqual(waits, allowed, f"{size}-B frames: {waits} calls waited for a slot ({busy:.2f}s of 6s on the caller; the gate allows {allowed} once)")

    def test_span_start_cleared_by_the_writer_mid_sample_does_not_raise(self):
        """Fresh pass on 925d275c, F2: `_note_pipe_full` read the span start
        twice — `is None`, then the subtraction — and the writer thread clears
        it unlocked, so a reset landing between the two reads raised
        `TypeError: float - NoneType` on the TX path (caught by Transport as an
        ERROR, with the drop uncounted and the stall check skipped). The
        interleave is replayed deterministically: the reset fires from the
        `_tx_full_last` read that sits between them."""
        fired = [0]
        class Interleave(type(self.iface)):
            @property
            def _tx_full_last(self):
                if not fired[0] and self.__dict__.get("_tx_full_since") is not None:
                    fired[0] = 1; self.__dict__["_tx_full_since"] = None
                return self.__dict__.get("_tx_full_last_v", 0.0)
            @_tx_full_last.setter
            def _tx_full_last(self, v): self.__dict__["_tx_full_last_v"] = v
        self.iface.__class__ = Interleave
        self.iface._pipe_full = lambda: True
        now = time.monotonic()
        self.iface._note_pipe_full(now)
        self.iface._note_pipe_full(now + 0.1)   # raised on 925d275c
        self.assertEqual(fired[0], 1, "the interleave was not exercised")
        self.assertFalse(self.iface._tx_congested)

    def test_a_quiet_gap_longer_than_the_span_restarts_it(self):
        """Fresh pass on 925d275c, F3: the third abandon rule — no timed-out
        put for TX_CONGEST_FULL_S means the caller was being served, so the
        span restarts — had no test; with it deleted the file still passed
        15/15. Samples are fed with explicit clocks so the leg is pinned on
        its own: a 0.8-span-long run of full samples, a 1.6-span quiet gap,
        then 0.6 span of full samples must NOT congest; 1.1 span must."""
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        self.iface._pipe_full = lambda: True
        t0 = time.monotonic()
        for f in (0.0, 0.2, 0.4, 0.6, 0.8): self.iface._note_pipe_full(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "congested after 0.8 of the span")
        for f in (2.4, 2.6, 3.0): self.iface._note_pipe_full(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "a span survived a quiet gap longer than TX_CONGEST_FULL_S")
        self.iface._note_pipe_full(t0 + 3.5 * S)
        self.assertTrue(self.iface._tx_congested, "a child behind for 1.1 spans after the gap did not congest")

    def test_interface_stats_carry_dropped_and_congested(self):
        """Same finding, the rnstatus half: `get_interface_stats` must expose
        the drop counter and the congested flag for a PipeInterface so the
        designed drops in the band are legible outside the log."""
        fake = mock.MagicMock(is_connected_to_shared_instance=False)
        # Set by the config loader on a real instance, read unguarded by the
        # stats builder; the fixture constructs the interface directly.
        for attr in ("announce_rate_target", "announce_rate_grace", "announce_rate_penalty",
                     "held_announces", "ic_burst_active", "ic_burst_activated",
                     "ic_pr_burst_active", "ic_pr_burst_activated"):
            if not hasattr(self.iface, attr): setattr(self.iface, attr, None)
        if not hasattr(self.iface, "mode"): self.iface.mode = RNS.Interfaces.Interface.Interface.MODE_FULL
        with mock.patch.object(RNS.Transport, "interfaces", [self.iface]):
            stats = RNS.Reticulum.get_interface_stats(fake)
        ifs = [i for i in stats["interfaces"] if i["short_name"] == self.iface.name]
        self.assertEqual(len(ifs), 1, stats["interfaces"])
        self.assertEqual(ifs[0].get("tx_dropped"), self.iface.tx_dropped)
        self.assertIs(ifs[0].get("tx_congested"), False)


if __name__ == "__main__":
    unittest.main()
