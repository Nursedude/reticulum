# SOURCE: copied from the mf.10 non-author pass's reviewer drill `gilsticky.py`
# (fresh pass on 8acd4d18, moc4, 2026-10-12; md5 51f34c4e3761 of the original),
# adapted for accept.sh legs 9 and 10 (F1 CPU contention, F2 idle-then-burst).
# Adaptations, each additive -- the original shape is AUX_BEFORE_PROVOKE=0:
#   * AUX_BEFORE_PROVOKE (default 1): burners start BEFORE the provoke and run
#     through the burst (the acceptance shape); 0 = the reviewer's order.
#   * QUIET_S accepts `stall+N`: TX_STALL_S read from the class under test + N.
#   * A tree-independent LOAD WITNESS: during the quiet the main thread waits on
#     an empty queue.Queue().get(timeout=TX_PUT_WAIT_S) -- the writer's own idle
#     primitive and period -- and records its wake gaps -> wake_gap_median_ms.
#     (A plain time.sleep witness read 0.4-0.9x the writer's poll gap on moc4:
#     one GIL re-take per wake vs several; calibration 2026-10-12.)
#   * PROVOKE_S (default 1.5, the original): the SIGSTOP budget to enter. With
#     burners started BEFORE the provoke, 8acd4d18 failed to enter within 1.5 s
#     in 3 of 4 calibration runs (9120f760: 4 of 4 entered). The writer's poll witness
#     (poll_gap_median_ms) exists only on trees with _writer_observes and only
#     while the flag is set, so it cannot say what load a GREEN run applied.
#   * Witnesses for F2: every `online` transition (offline_events) and every
#     RNS.log line saying `has not drained its stdin` (stall_lines).
# Usage: drill_gil.py <tree> episode|control
# env BURNERS (bytecode-loop threads, default 0), HOGS (long C-call GIL holders,
#     sum(range(HOG_N)), default 0), QUIET_S (default 5; `stall+1` allowed),
#     LOW_GAP (0 = no offers during the quiet; else seconds between sparse
#     offers), BURST_WITH_AUX (1 = keep the aux threads running through the
#     burst, default 1), AUX_BEFORE_PROVOKE (default 1)
import os, sys, time, tempfile, threading, signal, fcntl, termios, struct, statistics, queue
from unittest import mock
tree = sys.argv[1]; mode = sys.argv[2]
B = int(os.environ.get("BURNERS", "0")); H = int(os.environ.get("HOGS", "0")); HOG_N = int(os.environ.get("HOG_N", "3000000"))
LOW_GAP = float(os.environ.get("LOW_GAP", "0"))
AUXB = os.environ.get("BURST_WITH_AUX", "1") == "1"
AUXFIRST = os.environ.get("AUX_BEFORE_PROVOKE", "1") == "1"
PROVOKE_S = float(os.environ.get("PROVOKE_S", "1.5"))   # SIGSTOP budget to enter
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
_q = os.environ.get("QUIET_S", "5")
QUIET_S = PI.PipeInterface.TX_STALL_S + float(_q[6:] or 0) if _q.startswith("stall+") else float(_q)
STEP = PI.PipeInterface.TX_PUT_WAIT_S
stall_lines = []
_real_log = RNS.log
def _spy_log(msg, *a, **k):
    if "has not drained its stdin" in str(msg): stall_lines.append(round(time.monotonic(), 2))
    return _real_log(msg, *a, **k)
RNS.log = _spy_log
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "gil", "command": f"{sys.executable} -u {child}"})
fd = i.process.stdin.fileno()
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
trans = []; offline = []
class Spy(type(i)):
    @property
    def _tx_congested(self): return self.__dict__.get("_c", False)
    @_tx_congested.setter
    def _tx_congested(self, v):
        if bool(v) != self.__dict__.get("_c", False): trans.append((time.monotonic(), bool(v)))
        self.__dict__["_c"] = v
    @property
    def online(self): return self.__dict__.get("_on", False)
    @online.setter
    def online(self, v):
        if self.__dict__.get("_on") is True and not v: offline.append(time.monotonic())
        self.__dict__["_on"] = v
# poll witness: when the writer's idle poll ran (trees without _writer_observes record none)
polls = []
if hasattr(PI.PipeInterface, "_writer_observes"):
    orig = PI.PipeInterface._writer_observes
    def wo(self, now=None):
        polls.append(time.monotonic()); return orig(self, now)
    Spy._writer_observes = wo
_on0 = i.__dict__.pop("online", None)
i.__class__ = Spy
i.online = _on0 if _on0 is not None else True
i.process_outgoing(b"r" * 500); tr = time.monotonic()
while time.monotonic() - tr < 5 and (i.txb == 0 or occ() > 0): time.sleep(0.002)
t0 = time.monotonic()
def rel(t): return round(t - t0, 2)
stop = threading.Event()
def burn():
    x = 0
    while not stop.is_set():
        for _ in range(100000): x += 1
def hog():
    while not stop.is_set(): sum(range(HOG_N))
aux = [threading.Thread(target=burn, daemon=True) for _ in range(B)] + [threading.Thread(target=hog, daemon=True) for _ in range(H)]
if AUXFIRST:
    for t in aux: t.start()
if mode == "episode":
    os.kill(i.process.pid, signal.SIGSTOP)
    te = time.monotonic() + PROVOKE_S
    while time.monotonic() < te and not i._tx_congested: i.process_outgoing(b"s" * 500)
    print(f"provoke: congested={i._tx_congested} at t={time.monotonic()-t0:.2f} dropped={i.tx_dropped}")
    os.kill(i.process.pid, signal.SIGCONT)
if not AUXFIRST:
    for t in aux: t.start()
_idle_q = queue.Queue()
tq = time.monotonic(); pq0 = len(polls); n_off = 0; wakes = [tq]; next_off = tq
while time.monotonic() - tq < QUIET_S:
    if LOW_GAP > 0 and time.monotonic() >= next_off:
        i.process_outgoing(b"l" * 500); n_off += 1; next_off += LOW_GAP
    try: _idle_q.get(timeout=STEP)   # the writer's own idle primitive
    except queue.Empty: pass
    wakes.append(time.monotonic())
tq1 = time.monotonic()
qp = [p for p in polls[pq0:] if tq <= p <= tq1]
gaps = [b - a for a, b in zip(qp, qp[1:])]
wg = [b - a for a, b in zip(wakes, wakes[1:])]
pre = i._tx_congested
ex = [rel(t) for t, v in trans if not v]
gm = statistics.median(gaps) * 1000 if gaps else -1
g9 = sorted(gaps)[int(len(gaps) * 0.9)] * 1000 if gaps else -1
wm = statistics.median(wg) * 1000 if wg else -1
print(f"quiet {QUIET_S}s offers={n_off} aux=B{B}/H{H}: occ={occ()} qsize={i._txq.qsize()} congested_before_burst={pre} transitions={[(rel(t), v) for t, v in trans]} polls_in_quiet={len(qp)} poll_gap_median_ms={gm:.1f} poll_gap_p90_ms={g9:.1f} wake_gap_median_ms={wm:.1f} (step {STEP*1000:.0f} ms)")
if not AUXB: stop.set()
d1 = i.tx_dropped; worst = [0.0]
def work():
    for _ in range(2000):
        t = time.monotonic(); i.process_outgoing(b"b" * 500); worst[0] = max(worst[0], time.monotonic() - t)
th = threading.Thread(target=work, daemon=True); tb = time.monotonic(); th.start(); th.join(30)
took = time.monotonic() - tb
time.sleep(1.0); stop.set()
print(f"RESULT mode={mode} provoke_s={PROVOKE_S} B={B} H={H} LOW_GAP={LOW_GAP} QUIET_S={QUIET_S} aux_before_provoke={AUXFIRST} provoked={any(v for _, v in trans)} exits={ex} congested_before_burst={pre} burst_drops={i.tx_dropped-d1} burst_took={took:.2f} worst_call_ms={worst[0]*1000:.1f} congested_end={i._tx_congested} offline_events={len(offline)} stall_lines={len(stall_lines)} online_end={i.online} polls_in_quiet={len(qp)} poll_gap_median_ms={gm:.1f} poll_gap_p90_ms={g9:.1f} wake_gap_median_ms={wm:.1f}")
i.detached = True; i.respawn_delay = 3600
try: os.kill(i.process.pid, signal.SIGCONT)
except Exception: pass
i.process.kill()
