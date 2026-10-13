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
import inspect
import os
import select
import signal
import struct
import sys
import tempfile
import termios
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
    # "tick": every 0.25 s read whatever the pipe holds (up to 1 MB) — a child
    # that keeps up on a timer, so the pipe climbs for a tick and empties at
    # once (non-author pass on 9120f760, F4: on a 64 KB pipe at 160 kB/s each
    # tick peaks ~39 KB, above the 32 KB line, and the episode never closed).
    tick = sys.argv[1:2] == ["tick"]
    fd = sys.stdin.fileno()
    while True:
        if tick:
            time.sleep(0.25)
            if not os.read(fd, 1 << 20): break
        elif gulp:
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
        # The stock respawn loop does not look at `detached`: a killed child
        # is respawned every respawn_delay for the rest of the run, into a
        # tmp dir that no longer exists (~300 failed python3 starts per run on
        # 9120f760, ~1000 with the mf.9 tests — load 4-10 on a Pi 4 by the
        # end of the file, which the late, timing-sensitive tests then
        # measured). Park the loop instead.
        self.iface.respawn_delay = 3600
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
            i.respawn_delay = 3600   # see tearDown
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

    def _pipe_buffered(self):
        """Bytes sitting in the child's stdin pipe, from the write end."""
        return struct.unpack("i", fcntl.ioctl(self.iface.process.stdin.fileno(), termios.FIONREAD, b"\0\0\0\0"))[0]

    def _wait_child_reading(self):
        """One frame, then wait until the child has READ it (pipe empty). A
        just-spawned Python child takes 30-100 ms to reach its first read on a
        Pi 4; a burst offered before that fills pipe + queue and the first
        frame past them waits TX_PUT_WAIT_S for a child that is not reading
        yet, and is dropped — frame 321 = 64 + 256 + 1 on a 64 KB pipe, every
        run (4 KB-page canary, 2026-10-12). That measures startup, not the
        gate; the tests about a HEALTHY child start from a child that reads."""
        self.iface.process_outgoing(b"r" * 1000)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (self.iface.txb == 0 or self._pipe_buffered() > 0): time.sleep(0.002)
        self.assertEqual(self._pipe_buffered(), 0, "the child never read the first frame")

    def test_healthy_child_burst_drops_nothing(self):
        """Fresh pass F2: a tight burst of frames a draining child would have
        taken (stock: 0 drops, the kernel pipe absorbs it) must not be dropped
        because the queue filled faster than the writer got the GIL."""
        self._wait_child_reading()
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
        # episode opens (one caller: ~25), then none until low water. Up to
        # TWO spans, as in the gulp tests: a waited put whose slot comes the
        # instant the child frees a page reads the pipe writable and restarts
        # the span — correctly, the child was not behind at that sample — and
        # the partial span is paid again (non-author pass on 3182bd82: 39 and
        # 40 waits seen against a one-span bound of 31, 0 of 20 soak runs
        # otherwise; a re-arming gate costs a span per cycle, far above this).
        allowed = 2 * int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 6
        self.assertLessEqual(waits, allowed, f"{waits} calls waited for a slot ({busy:.2f}s of 5s on the caller; the gate allows {allowed} once)")

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
        # Two spans, once — see test_child_behind_at_a_fast_rate_still_congests.
        allowed = 2 * int(getattr(PI.PipeInterface, "TX_CONGEST_FULL_S", 0.0) / PI.PipeInterface.TX_PUT_WAIT_S) + 6
        self.assertLessEqual(waits, allowed, f"{size}-B frames: {waits} calls waited for a slot ({busy:.2f}s of 5s on the caller; the gate allows {allowed} once)")

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

    def test_gulp_child_behind_with_the_queue_under_low_water_stays_congested(self):
        """Non-author pass on 3182bd82 (2026-10-12): the pipe half of the
        caught-up rule had no test of its own — `qsize() <= TX_LOW_WATER` in
        _child_caught_up passed the whole file (the 1200 f/s gulp twins refill
        the queue past low water within ~0.3 s of a dump, so both rules hold
        the episode). The band that tells them apart: after a gulp empties
        the pipe and the writer dumps the queue, 500-B offers refill the
        pipe's free 256 frames in 256/rate s and carry the queue past 128 at
        384/rate s; between 512 and 768 f/s the pipe is full again BEFORE
        TX_CONGEST_FULL_S while the queue is still under low water AFTER it.
        Measured at 600 f/s (300 kB/s into a 200 kB/s child): the low-water
        rule closed the episode twice in 10 s about a child 1.3 s behind
        (111 waits); queue EMPTY and pipe writable held it (30-39 waits).
        Pinned on this kernel's 16 KB pages / 256 KB pipe; on 4 KB pages the
        queue dump does not fit the pipe and this shape pins nothing."""
        self._swap_iface({"name": "gulp", "command": self.cfg["command"] + " gulp 200000"})
        entered = self._spy_congestion()
        waits = 0
        t_end = time.monotonic() + 10.0; k = 0; t0 = time.monotonic()
        while time.monotonic() < t_end:
            k += 1
            t = time.monotonic(); self.iface.process_outgoing(b"g" * 500)
            if time.monotonic() - t >= 0.015: waits += 1
            rem = t0 + k / 600.0 - time.monotonic()
            if rem > 0: time.sleep(rem)
        self.assertGreater(self.iface.tx_dropped, 0, "the child was never behind; the shape is vacuous")
        self.assertNotEqual(entered, [], "a child that was behind never entered congested mode")
        self.assertEqual(len(entered), 1, f"congested {len(entered)}x in 10 s: the episode closed on a child still behind ({waits} waits)")

    def _note(self, now, waited=None):
        """Feed one sample to the gate with an explicit clock. `waited` is how
        long the put that sampled waited for its slot; default: the whole
        interval (several senders waiting back to back), so the tests that
        pin the span arithmetic keep their arithmetic. On a tree whose gate
        takes no `waited` (9120f760 and before) the sample is fed without it,
        so a test written for the bound FAILS there instead of ERRORing."""
        f = self.iface._note_pipe_full
        if "waited" in inspect.signature(f).parameters:
            f(now, waited=PI.PipeInterface.TX_CONGEST_FULL_S if waited is None else waited)
        else:
            f(now)

    def _park_writer(self):
        """Retire the writer thread so a sample sequence fed with an explicit
        clock is not raced by the writer's own checks on a live pipe."""
        self.iface._writer_gen += 1
        self.iface._writer.join(2)
        self.assertFalse(self.iface._writer.is_alive(), "writer did not park")

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
        self._note(now)
        self._note(now + 0.1)   # raised on 925d275c
        self.assertEqual(fired[0], 1, "the interleave was not exercised")
        self.assertFalse(self.iface._tx_congested)

    def test_a_quiet_gap_longer_than_the_span_restarts_it(self):
        """Fresh pass on 925d275c, F3: the third abandon rule — no timed-out
        put for TX_CONGEST_FULL_S means the caller was being served, so the
        span restarts — had no test; with it deleted the file still passed
        15/15. Samples are fed with explicit clocks so the leg is pinned on
        its own: a 0.8-span-long run of full samples, a 1.6-span quiet gap,
        then 0.6 span of full samples must NOT congest; 1.1 span must."""
        self._park_writer()
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        self.iface._pipe_full = lambda: True
        t0 = time.monotonic()
        for f in (0.0, 0.2, 0.4, 0.6, 0.8): self._note(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "congested after 0.8 of the span")
        for f in (2.4, 2.6, 3.0): self._note(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "a span survived a quiet gap longer than TX_CONGEST_FULL_S")
        self._note(t0 + 3.5 * S)
        self.assertTrue(self.iface._tx_congested, "a child behind for 1.1 spans after the gap did not congest")

    def test_a_burst_the_writer_absorbed_ends_the_span(self):
        """Non-author pass on 3182bd82 (2026-10-12): the writer's own abandon
        rule — after a completed write with the queue empty and the pipe
        writable, an open pipe-full span is cleared — had no test; with it
        deleted (`if False:`) the file passed 21/21, because every other shape
        also fed a waited put that found the pipe writable (the caller's rule)
        before the span could matter. Here no put waits between two short
        spans: 0.4 S of full samples, the burst absorbed by the real writer,
        then full samples again inside the quiet-gap window. The second span
        starts fresh; without the rule the two sum and congest at 1.1 S."""
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        t0 = time.monotonic()
        for f in (0.0, 0.2, 0.4): self._note(t0 + f * S)
        self.assertIsNotNone(self.iface._tx_full_since)
        # the burst is absorbed: the writer's next completed write finds the
        # queue empty and the pipe writable
        full[0] = False
        txb0 = self.iface.txb
        self.iface.process_outgoing(b"a" * 500)
        deadline = time.monotonic() + 5
        while self.iface.txb == txb0 and time.monotonic() < deadline: time.sleep(0.005)
        self.assertGreater(self.iface.txb, txb0, "the writer never wrote the frame")
        time.sleep(0.05)   # the writer's post-write check runs after txb is bumped
        full[0] = True
        self._note(t0 + 1.1 * S)
        self.assertFalse(self.iface._tx_congested, "congested on the sum of two spans separated by an absorbed burst")
        self._note(t0 + 1.5 * S)
        self.assertFalse(self.iface._tx_congested, "the span that began at 1.1 S is only 0.4 S old")
        self._note(t0 + 2.2 * S)
        self.assertTrue(self.iface._tx_congested, "a span 1.1 S old did not congest (the shape is vacuous)")

    def test_pipe_full_reads_occupancy_not_a_free_page(self):
        """4 KB-page canary (2026-10-12, moc4 + moc5): `select()` says a pipe
        is writable whenever ONE page is free, and on a 4 KB-page kernel a
        child behind at 800 kB/s frees a page every ~5 ms, so 75 % of the
        samples on a pipe that was 69-100 % full read writable, no span ever
        formed, and the caller waited 4.3-4.6 s of every 5 s (the mf.6
        saturation mf.7 claims to cure). Seven of the fleet's nine RNS boxes
        are 4 KB pages; the arc was built and reviewed on the two 16 KB Pi 5s.
        The kernel fact that does not depend on page size is how many BYTES
        are buffered (FIONREAD, readable from the write end on both kernels):
        a pipe three quarters full is backed up, whatever select() says."""
        os.kill(self.child_pid, signal.SIGSTOP)
        fd = self.iface.process.stdin.fileno()
        cap = fcntl.fcntl(fd, 1032)   # F_GETPIPE_SZ
        os.set_blocking(fd, False)
        try:
            written = 0
            while written < cap * 3 // 4:
                written += os.write(fd, b"\0" * min(4096, cap * 3 // 4 - written))
        finally:
            os.set_blocking(fd, True)
        self.assertGreaterEqual(self._pipe_buffered(), cap * 3 // 4)
        self.assertTrue(bool(select.select([], [fd], [], 0)[1]), "a quarter-empty pipe must still be writable: the shape is vacuous")
        self.assertTrue(self.iface._pipe_full(), f"a pipe {self._pipe_buffered()} of {cap} bytes full read as not full")

    def test_room_samples_clustered_in_time_do_not_abandon_the_span(self):
        """4 KB-page canary (2026-10-12): a child that is behind and reads in
        gulps empties the 64 KB pipe every ~0.3 s; the writer refills it in a
        few ms. The caller samples the pipe only on puts that waited, and the
        puts that waited through the full phase are all released in those few
        ms — so 20-50 samples read ROOM inside one gulp cycle against 5 that
        read full (one per 20 ms timeout), and a rule that abandons the span
        on any room sample never let it reach TX_CONGEST_FULL_S (moc4: 68-81
        waits vs 56 at 1000 B; the 500-B leg flaked). Evidence is weighed by
        the TIME it stood for, not by how many samples landed in it: 0.3 S of
        full samples one 0.1 S apart, then — one put-timeout (0.02 S) later —
        30 room samples inside 0.01 S, then full samples again. The cluster
        stood for ~0.03 S of room against 0.3 S of full, so the span survives;
        full time reaches S at 1.1 S (0.3 + 0.17 + 0.6), not at 0.9 S."""
        self._park_writer()
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        t0 = time.monotonic()
        for f in (0.0, 0.1, 0.2, 0.3): self._note(t0 + f * S)
        full[0] = False
        for i in range(30): self._note(t0 + 0.32 * S + i * 0.0003 * S)
        self.assertIsNotNone(self.iface._tx_full_since, "30 room samples spanning 0.01 S abandoned a 0.3 S span of full ones")
        full[0] = True
        for f in (0.5, 0.7, 0.9): self._note(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "congested before TX_CONGEST_FULL_S of full evidence (0.87 S)")
        self._note(t0 + 1.1 * S)
        self.assertTrue(self.iface._tx_congested, "a child behind for 1.1 S with one 10 ms cluster of room samples did not congest")

    def test_room_that_outweighs_full_in_time_abandons_the_span(self):
        """The other side of the time-weighted rule: a healthy child under GIL
        hogs leaves the pipe with room for most of the time the caller is
        waiting (the writer, not the child, is behind). When the time the
        pipe had room exceeds the time it was backed up, the span is over —
        however long it has been open. 0.5 S of full samples, 0.6 S of room
        (abandons at 1.1 S: 0.6 > 0.5), then 0.5 S of full: a gate that never
        abandons on room has 1.0 S of full time here and congests; the span
        must have started fresh at 1.2 S and hold 0.5 S. The same fresh span
        congests at 2.3 S (1.1 S), so the shape is not vacuous."""
        self._park_writer()
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        t0 = time.monotonic()
        for f in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5): self._note(t0 + f * S)
        full[0] = False
        for f in (0.7, 0.9, 1.1): self._note(t0 + f * S)
        self.assertIsNone(self.iface._tx_full_since, "0.6 S of room against 0.5 S of full did not abandon the span")
        full[0] = True
        for f in (1.2, 1.4, 1.6, 1.7): self._note(t0 + f * S)
        self.assertFalse(self.iface._tx_congested, "congested with 0.5 S of full evidence in the fresh span: the abandoned span's full time was carried")
        for f in (2.0, 2.3): self._note(t0 + f * S)
        self.assertTrue(self.iface._tx_congested, "a fresh 1.1 S span after the abandoned one did not congest (the shape is vacuous)")

    def test_caught_up_needs_an_empty_queue_and_a_pipe_with_room(self):
        """Both halves of `_child_caught_up`, pinned without a kernel: the
        gulp-at-600-f/s shape above tells "queue empty" from "queue under low
        water" only on 16 KB pages (on a 64 KB pipe the queue dump does not
        fit, so the `lowwater` mutant survived the whole file on moc4). With
        the pipe backed up an empty queue is NOT caught up; with room it is;
        with room and one frame still queued it is not."""
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        self.assertEqual(self.iface._txq.qsize(), 0)
        self.assertFalse(self.iface._child_caught_up(), "an empty queue in front of a backed-up pipe read as caught up")
        full[0] = False
        self.assertTrue(self.iface._child_caught_up())
        # park the writer so the frame stays queued for the sample
        self.iface._writer_gen += 1
        self.iface._writer.join(2)
        self.iface._txq.put_nowait(b"q")
        self.assertFalse(self.iface._child_caught_up(), "a queued frame in front of a pipe with room read as caught up")

    def test_a_put_that_found_the_queue_full_samples_however_short_its_wait(self):
        """4 KB-page canary (2026-10-12): a child behind at 800 kB/s on a 4 KB
        kernel frees 8 slots every ~5 ms, so a put that found the queue full
        gets its slot in under TX_PUT_WAIT_S / 2 and a wait-length rule never
        sampled the pipe. The queue being full on arrival IS the caller being
        throttled; the sample must happen whatever the wait turned out to be.
        Pinned with a queue that reports full and hands out the slot at once
        (on the real kernel the 800 kB/s shape also samples on jitter waits,
        so the `nothrottled` mutant survived the file on both kernels)."""
        noted = []
        self.iface._note_pipe_full = lambda now, *a, **k: noted.append(now)
        class InstantFullQueue:
            def full(self): return True
            def put(self, data, timeout=None): return None
            def put_nowait(self, data): return None
            def qsize(self): return PI.PipeInterface.TX_QUEUE_DEPTH
        self.iface._txq = InstantFullQueue()
        self.iface.process_outgoing(b"t" * 500)
        self.assertEqual(len(noted), 1, "a put that found the queue full and waited ~0 ms did not sample the pipe")

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

    # ---- 1.3.8+mf.9: non-author pass on 9120f760 (2026-10-12, moc4 4 KB) ----

    def _offer_at(self, bps, seconds, size=1000):
        """One sender offering `size`-B frames at `bps` for `seconds`, paced
        to a schedule (it does not sleep once it has fallen behind it)."""
        fs = bps / size; t0 = time.monotonic(); k = 0
        while time.monotonic() - t0 < seconds:
            k += 1; self.iface.process_outgoing(b"o" * size)
            rem = t0 + k / fs - time.monotonic()
            if rem > 0: time.sleep(rem)

    def _exit_offers(self, times, backed=lambda t: False, before=lambda t: None):
        """Feed congested-mode offers to the exit rule with an explicit clock;
        return the first offer time at which it said caught up, or None."""
        for t in times:
            before(t)
            self.iface._pipe_full = (lambda b: (lambda: b))(backed(t))
            if self.iface._caught_up_for(t): return t
        return None

    def _congested_parked(self):
        self._park_writer()
        self.iface._tx_congested = True
        self.iface._tx_clear_since = None
        return PI.PipeInterface.TX_CONGEST_FULL_S

    def test_quiet_between_pulses_is_not_credited_as_full(self):
        """F1 (non-author pass on 9120f760), the measured sequence replayed
        with an explicit clock: a healthy child fed 400 x 1000-B pulses every
        0.25 s (1.6 MB/s mean into 2 MB/s). Each pulse fills the 64 KB pipe
        for a few ms, the child empties it, and nothing waits in between. The
        gate credited the WHOLE gap since the previous sample to the state the
        pipe was in at the next one: full at 0.031-0.033, full again at 0.296
        -> 0.263 s of quiet booked as full, congested at 0.544 with 0 drops,
        then 1,204 drops (1 of 5 live runs). Credit is bounded by the time the
        put that sampled actually WAITED: what was observed, not the gap. The
        writer is parked here so this pins the per-put bound on its own."""
        self._park_writer()
        W = PI.PipeInterface.TX_PUT_WAIT_S
        self.iface._pipe_full = lambda: True
        t0 = 1000.0; waits = 0.0
        for k in range(8):
            p = t0 + 0.25 * k
            for dt, w in ((0.031, W), (0.033, 0.002), (0.046, 0.013)):
                self._note(p + dt, waited=w)
                if k or dt > 0.031: waits += w
        self.assertFalse(self.iface._tx_congested, f"a healthy pulse child congested: {self.iface._tx_full_time:.3f}s booked as full against {waits:.3f}s of observed waits")
        self.assertLessEqual(self.iface._tx_full_time, waits + 1e-9, "full time exceeds the time puts were seen waiting: the quiet between pulses was credited")

    def test_writer_idle_with_a_drained_pipe_ends_the_span(self):
        """F1, the other half: the writer's caught-up check ran only AFTER a
        write, so a burst's last write left the pipe over half full and the
        quiet that followed was never looked at — `_tx_full_since` stayed set
        over `occ=0 qsize=0` between pulses (review drill, every pulse line).
        A writer that goes idle with a span open looks at the pipe while it
        waits, so a pipe that drains with nothing queued ends the span without
        any put."""
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        self._note(time.monotonic(), waited=PI.PipeInterface.TX_PUT_WAIT_S)
        self.assertIsNotNone(self.iface._tx_full_since)
        full[0] = False
        # The writer may have entered its idle wait (0.5 s) before the span
        # opened; from then on it looks every TX_PUT_WAIT_S.
        deadline = time.monotonic() + 1.0
        while self.iface._tx_full_since is not None and time.monotonic() < deadline: time.sleep(0.005)
        self.assertIsNone(self.iface._tx_full_since, "an idle writer left a span open over a pipe with room and an empty queue")

    def test_the_writer_ends_the_span_after_a_write_not_only_when_idle(self):
        """The writer's post-write check, pinned apart from the new idle one:
        a writer fed faster than its idle poll never times out, so only the
        check after a write can see a child that took everything. The idle
        poll is pushed out of reach (TX_PUT_WAIT_S 10 s); one frame written
        into a pipe with room, with nothing else queued, ends the span (mf.9
        matrix: with the idle check in place `noreset` survived the file)."""
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        with mock.patch.object(PI.PipeInterface, "TX_PUT_WAIT_S", 10.0):
            self._note(time.monotonic(), waited=0.02)
            self.assertIsNotNone(self.iface._tx_full_since)
            full[0] = False
            txb0 = self.iface.txb
            self.iface.process_outgoing(b"a" * 500)
            deadline = time.monotonic() + 1.0
            while self.iface._tx_full_since is not None and time.monotonic() < deadline: time.sleep(0.005)
            self.assertGreater(self.iface.txb, txb0, "the writer never wrote the frame")
            self.assertIsNone(self.iface._tx_full_since, "a write that left the queue empty and the pipe with room did not end the span")

    def test_healthy_pulse_child_between_pulses_never_congests(self):
        """F1 live, the review's pulse shape: 4 senders x (pipe + ~336 kB) of
        1000-B frames per pulse into a 2 MB/s child, the next pulse after the
        pipe has drained. Invariants per pulse: a drained pipe with an empty
        queue leaves NO span open (the quiet is observed, not carried), the
        next pulse's first sample carries no more full credit than a put can
        wait, and the child is never called behind."""
        self._swap_iface({"name": "pulse", "command": self.cfg["command"] + " rate 2000000"})
        self._wait_child_reading()
        cap = fcntl.fcntl(self.iface.process.stdin.fileno(), 1032)
        per = (cap // 1000 + 336) // 4
        W = PI.PipeInterface.TX_PUT_WAIT_S
        entered = self._spy_congestion()
        sampled = [0]; first = [None]; carried = []; big = []
        real = self.iface._note_pipe_full
        def spy(now, *a, **k):
            sampled[0] += 1
            if first[0] is None:
                first[0] = 1; open_ = self.iface._tx_full_since is not None; f0 = self.iface._tx_full_time
                r = real(now, *a, **k)
                if open_ and self.iface._tx_full_since is not None and self.iface._tx_full_time - f0 > 2 * W:
                    big.append(round(self.iface._tx_full_time - f0, 3))
                return r
            return real(now, *a, **k)
        self.iface._note_pipe_full = spy
        for k in range(8):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and (self._pipe_buffered() > 0 or self.iface._txq.qsize() > 0): time.sleep(0.002)
            time.sleep(0.1)
            if self.iface._tx_full_since is not None: carried.append(k)
            first[0] = None
            ss = [threading.Thread(target=lambda: [self.iface.process_outgoing(b"p" * 1000) for _ in range(per)], daemon=True) for _ in range(4)]
            for t in ss: t.start()
            for t in ss: t.join(10)
        self.assertGreater(sampled[0], 0, "no put ever waited: the pulse never filled pipe + queue and the shape is vacuous")
        self.assertEqual(carried, [], f"pulses {carried} began with a span still open over a drained pipe and an empty queue")
        self.assertEqual(big, [], f"a pulse's first sample credited {big} s of full time (a put waits at most {W} s)")
        self.assertEqual(entered, [], f"a healthy pulse child was called behind ({self.iface.tx_dropped} dropped)")
        self.assertLessEqual(self.iface.tx_dropped, 8, f"{self.iface.tx_dropped} frames dropped from 8 pulses into a healthy child")

    def test_tick_child_that_keeps_up_exits_congestion(self):
        """F4 (non-author pass on 9120f760, drill_tick.py): a child that reads
        everything every 0.25 s, provoked behind at 1.5x what it can take,
        then held at 0.625x — it keeps up, drops go flat, yet each tick the
        pipe climbs past half (64 KB pipe at 160 kB/s: peaks ~39 KB vs the
        32 KB line), ~28 of 160 offers/s landed there and reset the old
        every-offer exit run, and the episode never closed (10 s, drops flat
        at 175). Exit is decided on what the domain loses: no drop, the queue
        at or under low water, and the pipe under half for most of the time,
        across TX_CONGEST_FULL_S of offers."""
        self._swap_iface({"name": "tick", "command": self.cfg["command"] + " tick"})
        self._wait_child_reading()
        per_s = fcntl.fcntl(self.iface.process.stdin.fileno(), 1032) / 0.25
        with mock.patch.object(PI.PipeInterface, "TX_STALL_S", 3600.0):
            self._offer_at(1.5 * per_s, 3.0)
            self.assertTrue(self.iface.tx_congested, "the tick child was never behind; the shape is vacuous")
            t_hold = time.monotonic(); exit_at = None; d_exit = None
            fs = 0.625 * per_s / 1000; k = 0
            while time.monotonic() - t_hold < 8.0 and not (exit_at is not None and time.monotonic() - t_hold > exit_at + 1.0):
                k += 1; self.iface.process_outgoing(b"o" * 1000)
                if exit_at is None and not self.iface.tx_congested:
                    exit_at = time.monotonic() - t_hold; d_exit = self.iface.tx_dropped
                rem = t_hold + k / fs - time.monotonic()
                if rem > 0: time.sleep(rem)
        self.assertIsNotNone(exit_at, f"a tick child keeping up at {0.625 * per_s / 1000:.0f} kB/s stayed congested for 8 s ({self.iface.tx_dropped} dropped)")
        self.assertLessEqual(exit_at, 6.0, f"exit took {exit_at:.2f}s of the hold")
        self.assertEqual(self.iface.tx_dropped, d_exit, f"frames dropped in the 1 s after the episode closed at {exit_at:.2f}s of the hold: the child was not caught up (loadavg {os.getloadavg()[0]:.2f})")

    def test_tick_child_held_behind_stays_congested(self):
        """The control for the tick exit: the same child held at 1.5x what it
        can take must stay congested — one episode, no exit line."""
        self._swap_iface({"name": "tickb", "command": self.cfg["command"] + " tick"})
        self._wait_child_reading()
        per_s = fcntl.fcntl(self.iface.process.stdin.fileno(), 1032) / 0.25
        entered = self._spy_congestion()
        with mock.patch.object(PI.PipeInterface, "TX_STALL_S", 3600.0):
            self._offer_at(1.5 * per_s, 7.0)
        self.assertEqual(len(entered), 1, f"a tick child held behind congested {len(entered)}x in 7 s")
        self.assertTrue(self.iface.tx_congested, "a tick child held behind was called caught up")

    def test_exit_needs_room_for_most_of_the_window(self):
        """F4 pinned without a kernel: offers every 0.05 S, the pipe over half
        full at one in five of them (the tick child's shape), no drop, queue
        empty: the episode closes after one window of TX_CONGEST_FULL_S."""
        S = self._congested_parked()
        times = [1000.0 + i * 0.05 * S for i in range(60)]
        at = self._exit_offers(times, backed=lambda t: round((t - 1000.0) / (0.05 * S)) % 5 == 4)
        self.assertIsNotNone(at, "pipe under half for 80 % of 3 S of offers, no drops: never caught up")
        self.assertGreaterEqual(at - 1000.0, S - 1e-9, "caught up before a whole window of offers")
        self.assertLessEqual(at - 1000.0, 1.5 * S, f"caught up only at {(at - 1000.0) / S:.2f} S")

    def test_exit_holds_while_the_pipe_stays_backed_up_without_drops(self):
        """The pipe half of the exit rule: a child whose pipe is over half
        full at 60 % of the offers is still holding a backlog (the gulp child
        on 16 KB pages after a queue dump), drops or not."""
        S = self._congested_parked()
        times = [1000.0 + i * 0.05 * S for i in range(80)]
        at = self._exit_offers(times, backed=lambda t: round((t - 1000.0) / (0.05 * S)) % 5 < 3)
        self.assertIsNone(at, f"caught up at {at} with the pipe backed up at 60 % of the offers")

    def test_a_drop_restarts_the_exit_window(self):
        """A frame dropped is the domain's loss: the window restarts at the
        offer that sees it, so a whole TX_CONGEST_FULL_S without one is owed."""
        S = self._congested_parked()
        times = [1000.0 + i * 0.05 * S for i in range(60)]
        def drop(t):
            if abs(t - (1000.0 + 0.4 * S)) < 1e-9: self.iface.tx_dropped += 1
        at = self._exit_offers(times, before=drop)
        self.assertIsNotNone(at, "never caught up after the drop")
        self.assertGreaterEqual(at - 1000.0, 1.4 * S - 1e-9, f"caught up at {(at - 1000.0) / S:.2f} S, less than a window after the drop at 0.4 S")

    def test_a_queue_over_low_water_restarts_the_exit_window(self):
        """The queue half: an offer that finds the queue over TX_LOW_WATER is
        not a caught-up child, however the pipe looks."""
        S = self._congested_parked()
        times = [1000.0 + i * 0.05 * S for i in range(60)]
        def stuff(t):
            q = self.iface._txq
            if abs(t - (1000.0 + 0.4 * S)) < 1e-9:
                for _ in range(PI.PipeInterface.TX_LOW_WATER + 1): q.put_nowait(b"q")
            elif abs(t - (1000.0 + 0.45 * S)) < 1e-9:
                while not q.empty(): q.get_nowait()
        at = self._exit_offers(times, before=stuff)
        self.assertIsNotNone(at, "never caught up after the queue drained")
        self.assertGreaterEqual(at - 1000.0, 1.4 * S - 1e-9, f"caught up at {(at - 1000.0) / S:.2f} S with the queue over low water at 0.4 S")

    def test_a_quiet_gap_between_offers_restarts_the_exit_window(self):
        """No offers for longer than TX_CONGEST_FULL_S is unobserved time,
        not caught-up time: the window restarts at the next offer (quiet is
        not behind, but it is not evidence either; the flag holds until offers
        show it)."""
        S = self._congested_parked()
        times = [1000.0] + [1000.0 + 1.2 * S + i * 0.05 * S for i in range(40)]
        at = self._exit_offers(times)
        self.assertIsNotNone(at, "never caught up after the quiet")
        self.assertGreaterEqual(at - 1000.0, 2.2 * S - 1e-9, f"caught up at {(at - 1000.0) / S:.2f} S: the quiet gap was counted as caught up")

    def test_a_late_offer_does_not_book_the_gap_before_it_as_room(self):
        """The F1 class on the exit side: an offer observes one instant. A
        sender descheduled for most of a window that offers just after the
        child read must not book the whole gap as room. Offers every 0.05 S
        see the pipe backed up for 0.6 S, then one offer 0.9 S later sees
        room: one offer, not 0.9 S of evidence — no exit at it."""
        S = self._congested_parked()
        times = [1000.0 + i * 0.05 * S for i in range(13)] + [1000.0 + 1.5 * S]
        at = self._exit_offers(times, backed=lambda t: t < 1000.0 + 0.65 * S)
        self.assertIsNone(at, f"caught up at {at}: a single late offer outweighed 0.6 S of backed-up offers")

    def test_a_sample_exactly_one_span_later_still_accrues(self):
        """The quiet-gap boundary (review: `>`-vs-`>=` unpinned): a gap of
        exactly TX_CONGEST_FULL_S continues the span; only a LONGER one
        restarts it. Exact binary clock values, so the gap is exactly S."""
        self._park_writer()
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        self.iface._pipe_full = lambda: True
        self._note(1000.0); self._note(1000.0 + S, waited=S)
        self.assertTrue(self.iface._tx_congested, "a gap of exactly S restarted the span")

    def test_room_equal_to_full_keeps_the_span(self):
        """The abandon boundary: room time EQUAL to full time does not end
        the span; only room that outweighs full does."""
        self._park_writer()
        full = [True]
        self.iface._pipe_full = lambda: full[0]
        self._note(1000.0); self._note(1000.125)
        full[0] = False
        self._note(1000.25)
        self.assertIsNotNone(self.iface._tx_full_since, "room equal to full in time abandoned the span")

    def test_pipe_full_fraction_has_a_lower_side(self):
        """The 0.5 line pinned from below (review: TX_PIPE_FULL_FRACTION 0.25
        passed all 28 on 4 KB): a pipe 35 % full is NOT backed up, 65 % is."""
        os.kill(self.child_pid, signal.SIGSTOP)
        fd = self.iface.process.stdin.fileno()
        cap = fcntl.fcntl(fd, 1032)
        os.set_blocking(fd, False)
        try:
            def fill_to(n):
                while self._pipe_buffered() < n: os.write(fd, b"\0" * min(4096, n - self._pipe_buffered()))
            fill_to(cap * 35 // 100)
            self.assertFalse(self.iface._pipe_full(), f"a pipe {self._pipe_buffered()} of {cap} bytes full read as backed up")
            fill_to(cap * 65 // 100)
            self.assertTrue(self.iface._pipe_full(), f"a pipe {self._pipe_buffered()} of {cap} bytes full read as not backed up")
        finally:
            os.set_blocking(fd, True)

    def test_a_sample_from_the_past_credits_nothing(self):
        """Two senders take `now` before the gate serializes them, so a sample
        can arrive with a clock older than the last one: it credits nothing,
        and does not move the span's clock backward (review: the negative-gap
        clamp was unpinned)."""
        self._park_writer()
        S = PI.PipeInterface.TX_CONGEST_FULL_S
        self.iface._pipe_full = lambda: True
        self._note(1000.0); self._note(1000.0 + 0.6 * S)
        self._note(1000.0 + 0.2 * S)
        self.assertFalse(self.iface._tx_congested)
        self._note(1000.0 + 1.0 * S)
        self.assertTrue(self.iface._tx_congested, f"full time {self.iface._tx_full_time / S:.2f} S after 1.0 S of full samples: a stale sample subtracted time")

    def test_concurrent_samples_never_credit_more_than_wall_time(self):
        """F5 (non-author pass on 9120f760): two senders released together
        both read the previous sample's clock and both credited the gap from
        it. Replayed deterministically: reading the span's clock yields the
        GIL for 5 ms, so without serialization every sender reads the same
        value. Full + room time must never exceed the span's wall time."""
        self._park_writer()
        class SlowLast(type(self.iface)):
            @property
            def _tx_full_last(self):
                v = self.__dict__.get("_v", 0.0); time.sleep(0.005); return v
            @_tx_full_last.setter
            def _tx_full_last(self, v): self.__dict__["_v"] = v
        self.iface.__class__ = SlowLast
        self.iface._pipe_full = lambda: True
        with mock.patch.object(PI.PipeInterface, "TX_CONGEST_FULL_S", 60.0):
            nows = [time.monotonic()]; self._note(nows[0])
            since = self.iface._tx_full_since
            def send():
                for _ in range(10):
                    n = time.monotonic(); nows.append(n); self._note(n, waited=60.0)
            ss = [threading.Thread(target=send, daemon=True) for _ in range(4)]
            for t in ss: t.start()
            for t in ss: t.join(10)
            self.assertEqual(self.iface._tx_full_since, since, "the span restarted; the shape is vacuous")
            wall = max(nows) - since
            booked = self.iface._tx_full_time + self.iface._tx_room_time
        self.assertLessEqual(booked, wall + 1e-6, f"{booked:.3f}s booked over a span {wall:.3f}s long")

    def test_a_wall_clock_that_stops_cannot_hide_a_stall(self):
        """The gate's durations are monotonic (RTC-less Pis; fake-hwclock and
        NTP steps move the wall clock — hfm #6; review mutant `wallclock`
        survived the file). With the wall clock frozen, a stopped child still
        takes the interface offline after TX_STALL_S."""
        with mock.patch("time.time", return_value=1.0e9):
            os.kill(self.child_pid, signal.SIGSTOP)
            self._fill_pipe()
            deadline = time.monotonic() + PI.PipeInterface.TX_STALL_S + 5
            while self.iface.online and time.monotonic() < deadline:
                self.iface.process_outgoing(b"y" * 1000); time.sleep(0.2)
            self.assertFalse(self.iface.online, "a frozen wall clock hid a stalled child")

    def test_import_without_fcntl_and_termios(self):
        """F2 (non-author pass on 9120f760): `import fcntl` / `import termios`
        sat unguarded at module top and Reticulum star-imports every
        interface, so `import RNS` failed where they do not exist (Windows).
        Without them the gate falls back to the free-page poll, COUNTED."""
        import subprocess, json
        script = textwrap.dedent("""
            import json, subprocess, sys
            sys.modules["fcntl"] = None; sys.modules["termios"] = None
            sys.path.insert(0, sys.argv[1])
            import RNS
            from RNS.Interfaces import PipeInterface as PI
            i = PI.PipeInterface.__new__(PI.PipeInterface)
            i.name = "nofcntl"; i._pipe_cap = None; i._pipe_cap_warned = False; i.tx_probe_errors = 0
            i.process = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            full = [i._pipe_full(), i._pipe_full()]
            i.process.kill()
            print(json.dumps({"fcntl": PI.fcntl is None, "full": full, "errors": i.tx_probe_errors}))
        """)
        path = os.path.join(self.tmp.name, "nofcntl.py")
        with open(path, "w") as f: f.write(script)
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        r = subprocess.run([sys.executable, path, root], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, f"import RNS without fcntl/termios failed: {r.stderr.strip().splitlines()[-1:]}")
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out, {"fcntl": True, "full": [False, False], "errors": 1}, out)

    def test_unreadable_pipe_capacity_is_counted_and_logged_once(self):
        """F3 (non-author pass on 9120f760): an F_GETPIPE_SZ failure cached
        capacity 0 and fell back to the select() free-page poll — the signal
        this arc proved wrong on 4 KB pages — with no counter and no log. The
        fallback is counted once per child and named once at WARNING."""
        lines = []
        real_log = RNS.log
        def spy(msg, level=None, *a, **k):
            if str(self.iface) in str(msg): lines.append((str(msg), level))
            return real_log(msg, level, *a, **k) if level is not None else real_log(msg, *a, **k)
        self.iface._pipe_cap = None
        real_fcntl = PI.fcntl.fcntl
        def fail(fd, cmd, *a):
            if cmd == PI.PipeInterface.F_GETPIPE_SZ: raise OSError(22, "Invalid argument")
            return real_fcntl(fd, cmd, *a)
        with mock.patch.object(PI.fcntl, "fcntl", fail), mock.patch.object(RNS, "log", spy):
            self.iface._pipe_full(); self.iface._pipe_full()
        self.assertEqual(self.iface.tx_probe_errors, 1, "an unreadable pipe capacity was not counted (once)")
        warn = [l for l, lv in lines if lv == RNS.LOG_WARNING and "select" in l]
        self.assertEqual(len(warn), 1, f"the fallback was not named once at WARNING: {lines}")

    def test_interface_stats_carry_probe_errors(self):
        """F3, the rnstatus half: the probe-error counter has a reader."""
        fake = mock.MagicMock(is_connected_to_shared_instance=False)
        for attr in ("announce_rate_target", "announce_rate_grace", "announce_rate_penalty",
                     "held_announces", "ic_burst_active", "ic_burst_activated",
                     "ic_pr_burst_active", "ic_pr_burst_activated"):
            if not hasattr(self.iface, attr): setattr(self.iface, attr, None)
        if not hasattr(self.iface, "mode"): self.iface.mode = RNS.Interfaces.Interface.Interface.MODE_FULL
        self.iface.tx_probe_errors = 3
        with mock.patch.object(RNS.Transport, "interfaces", [self.iface]):
            stats = RNS.Reticulum.get_interface_stats(fake)
        ifs = [i for i in stats["interfaces"] if i["short_name"] == self.iface.name]
        self.assertEqual(ifs[0].get("tx_probe_errors"), 3)


if __name__ == "__main__":
    unittest.main()
