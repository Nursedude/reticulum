# F1 drill from the non-author pass on 9120f760 (2026-10-12): a healthy child (RATE B/s) fed
# 4 x 100 x 1000-B frames every PERIOD s. env PERIOD (s), RATE (child B/s), NP (pulses),
# LOG=1 -> per-pulse state lines. Usage: drill_pulse_x.py <tree>. mf.9: spy takes the gate's
# (now, waited) signature.
import os, sys, time, tempfile, threading, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]
PERIOD = float(os.environ.get("PERIOD", "0.25")); RATE = os.environ.get("RATE", "2000000"); NP = int(os.environ.get("NP", "20")); LOG = os.environ.get("LOG") == "1"
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "pulsex", "command": f"{sys.executable} -u {child} rate {RATE}"})
time.sleep(0.5); entered = []
fd = i.process.stdin.fileno()
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
orig = PI.PipeInterface._note_pipe_full
def spy(self, now, *a):
    was = self._tx_congested; orig(self, now, *a)
    if self._tx_congested and not was: entered.append(round(now - t0, 2))
i._note_pipe_full = spy.__get__(i)
def st(tag, k):
    now = time.monotonic(); fs = i._tx_full_since
    print(f"  {tag} k={k} t={now-t0:.3f} occ={occ()} qsize={i._txq.qsize()} full_since={'None' if fs is None else round(fs-t0,3)} full_last={round(i._tx_full_last-t0,3)} full_time={i._tx_full_time:.3f} room_time={i._tx_room_time:.3f} congested={i._tx_congested} dropped={i.tx_dropped}")
t0 = time.monotonic()
print(f"PERIOD={PERIOD} RATE={RATE} NP={NP} cap={i._pipe_cap}")
for k in range(NP):
    if LOG: st("pre ", k)
    ss = [threading.Thread(target=lambda: [i.process_outgoing(b"p" * 1000) for _ in range(100)]) for _ in range(4)]
    [s.start() for s in ss]; [s.join(10) for s in ss]
    if LOG: st("post", k)
    rem = t0 + (k + 1) * PERIOD - time.monotonic()
    if rem > 0: time.sleep(rem)
print(f"pulsex: entered_at={entered} congested_at_end={i.tx_congested} dropped={i.tx_dropped} probe_errors={i.tx_probe_errors} cap={i._pipe_cap} elapsed={time.monotonic()-t0:.2f}")
i.detached = True; i.process.kill()
