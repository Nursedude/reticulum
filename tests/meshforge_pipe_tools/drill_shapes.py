# mf.10 acceptance shapes. No RNS instance; the fixture is the fork test file's.
# Usage: drill_shapes.py <tree> <mode>
#   quietburst  — SIGSTOP-provoke an episode, SIGCONT, then NO offers at all for
#                 QUIET_S (3 s), then 2,000 x 500 B tight. Healthy: the flag is
#                 cleared before the burst and the burst drops 0. (mf.9 holds the
#                 flag through quiet, so the burst meets no back-pressure.)
#   quietctl    — the same with no episode (the burst's own baseline).
#   hung        — a healthy child offered 500 B at 1000 f/s; SIGSTOP at 1 s, held
#                 HOLD_S (3 s, under TX_STALL_S), SIGCONT, offers continue 3 s.
#                 Healthy: enters within ~0.6 s of the queue first reading full,
#                 never exits while stopped, exits within ~1 s of the child having
#                 drained (queue empty and pipe under the line).
#   behind      — a `rate 100000` child offered 500 B at 600 f/s (300 kB/s) for
#                 8 s. Healthy: one entry, no exit, congested at the end.
# Every line that starts RESULT is what accept_verdict.py reads.
import os, sys, time, tempfile, threading, signal, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]; mode = sys.argv[2]
QUIET_S = float(os.environ.get("QUIET_S", "3")); HOLD_S = float(os.environ.get("HOLD_S", "3"))
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
arg = " rate 100000" if mode == "behind" else ""
i = PI.PipeInterface(T.FakeOwner(), {"name": mode, "command": f"{sys.executable} -u {child}{arg}"})
fd = i.process.stdin.fileno(); cap = fcntl.fcntl(fd, 1032)
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
trans = []   # (t, True=entered / False=exited), recorded by the code itself
class Spy(type(i)):
    @property
    def _tx_congested(self): return self.__dict__.get("_c", False)
    @_tx_congested.setter
    def _tx_congested(self, v):
        if bool(v) != self.__dict__.get("_c", False): trans.append((time.monotonic(), bool(v)))
        self.__dict__["_c"] = v
i.__class__ = Spy
# readiness: the child has read a frame (the 4 KB startup drop is not the gate)
i.process_outgoing(b"r" * 500); tr = time.monotonic()
while time.monotonic() - tr < 5 and (i.txb == 0 or occ() > 0): time.sleep(0.002)
t0 = time.monotonic()
def rel(t): return round(t - t0, 2)

def burst():
    d1 = i.tx_dropped; worst = [0.0]
    def work():
        for _ in range(2000):
            t = time.monotonic(); i.process_outgoing(b"b" * 500); worst[0] = max(worst[0], time.monotonic() - t)
    th = threading.Thread(target=work, daemon=True); th.start(); th.join(20)
    time.sleep(1.0)
    return i.tx_dropped - d1, worst[0]

if mode in ("quietburst", "quietctl"):
    if mode == "quietburst":
        os.kill(i.process.pid, signal.SIGSTOP)
        te = time.monotonic() + 1.5
        while time.monotonic() < te and not i._tx_congested: i.process_outgoing(b"s" * 500)
        print(f"provoke: congested={i._tx_congested} at t={time.monotonic()-t0:.2f} dropped={i.tx_dropped}")
        os.kill(i.process.pid, signal.SIGCONT); tc = time.monotonic()
    else:
        tc = time.monotonic()
    time.sleep(QUIET_S)
    pre = i._tx_congested
    print(f"quiet {QUIET_S}s, no offers: occ={occ()} qsize={i._txq.qsize()} congested_before_burst={pre} transitions={[(rel(t), v) for t, v in trans]}")
    d, worst = burst()
    print(f"RESULT mode={mode} provoked={any(v for _, v in trans)} congested_before_burst={pre} burst_drops={d} worst_call_ms={worst*1000:.1f} congested_end={i._tx_congested}")

elif mode == "hung":
    first_full = [None]; drained = None; k = 0; ts = time.monotonic()
    stop_at = ts + 1.0; cont_at = stop_at + HOLD_S; end_at = cont_at + 3.0; stopped = False; resumed = False
    while time.monotonic() < end_at:
        now = time.monotonic()
        if not stopped and now >= stop_at: os.kill(i.process.pid, signal.SIGSTOP); stopped = True; t_stop = now
        if stopped and not resumed and now >= cont_at: os.kill(i.process.pid, signal.SIGCONT); resumed = True; t_cont = now
        if stopped and first_full[0] is None and i._txq.full(): first_full[0] = now
        if resumed and drained is None and i._txq.qsize() == 0 and occ() < cap * 0.5: drained = time.monotonic()
        k += 1; i.process_outgoing(b"h" * 500)
        rem = ts + k / 1000.0 - time.monotonic()
        if rem > 0: time.sleep(rem)
    ent = [t for t, v in trans if v]; ex = [t for t, v in trans if not v]
    ex_stopped = [t for t in ex if t_stop <= t < t_cont]
    e = ent[0] if ent else None
    x_after = [t for t in ex if t >= t_cont]
    print(f"RESULT mode=hung entries={len(ent)} entry_after_stop={None if e is None else round(e - t_stop, 2)} "
          f"entry_after_queue_full={None if e is None or first_full[0] is None else round(e - first_full[0], 2)} "
          f"exits_while_stopped={len(ex_stopped)} drained_after_cont={None if drained is None else round(drained - t_cont, 2)} "
          f"exit_after_drained={None if not x_after or drained is None else round(x_after[0] - drained, 2)} "
          f"congested_end={i._tx_congested} dropped={i.tx_dropped} online={i.online}")

elif mode == "behind":
    k = 0; ts = time.monotonic()
    while time.monotonic() - ts < 8.0:
        k += 1; i.process_outgoing(b"x" * 500)
        rem = ts + k / 600.0 - time.monotonic()
        if rem > 0: time.sleep(rem)
    ent = [rel(t) for t, v in trans if v]; ex = [rel(t) for t, v in trans if not v]
    print(f"RESULT mode=behind entries={len(ent)} entered_at={ent} exits={len(ex)} exited_at={ex} congested_end={i._tx_congested} dropped={i.tx_dropped} offered={k}")

i.detached = True; i.respawn_delay = 3600
try: os.kill(i.process.pid, signal.SIGCONT)
except Exception: pass
i.process.kill()
