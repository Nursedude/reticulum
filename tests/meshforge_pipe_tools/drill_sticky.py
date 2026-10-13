# Copied verbatim (apart from this header) from the mf.9 non-author pass's drill
# (~/mf9-review/drills/sticky.py on moc4) for mf.10's acceptance script.
# Reviewer drill (mf.9 non-author pass): does an episode close when post-episode
# traffic is sparse (gaps > TX_CONGEST_FULL_S between offers), and what does a
# later burst into a HEALTHY child cost while the flag is stuck?
# Usage: sticky.py <tree> <mode> ; mode = episode (provoke via SIGSTOP) | control (no episode)
# env LOW_GAP (s between low-rate offers, default 0.7), LOW_S (default 10)
import os, sys, time, tempfile, threading, signal, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]; mode = sys.argv[2]
LOW_GAP = float(os.environ.get("LOW_GAP", "0.7")); LOW_S = float(os.environ.get("LOW_S", "10"))
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "sticky", "command": f"{sys.executable} -u {child}"})
fd = i.process.stdin.fileno()
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
# readiness: child has read a frame
i.process_outgoing(b"r" * 500); tr = time.monotonic()
while time.monotonic() - tr < 5 and (i.txb == 0 or occ() > 0): time.sleep(0.002)
t0 = time.monotonic()
if mode == "episode":
    os.kill(i.process.pid, signal.SIGSTOP)
    te = time.monotonic() + 1.5
    while time.monotonic() < te and not i._tx_congested: i.process_outgoing(b"s" * 500)
    print(f"provoke: congested={i._tx_congested} at t={time.monotonic()-t0:.2f} dropped={i.tx_dropped}")
    os.kill(i.process.pid, signal.SIGCONT)
    time.sleep(1.0)
    print(f"after drain: occ={occ()} qsize={i._txq.qsize()} congested={i._tx_congested} span_open={i._tx_full_since is not None}")
d0 = i.tx_dropped
n = int(LOW_S / LOW_GAP)
for k in range(n):
    i.process_outgoing(b"l" * 500); time.sleep(LOW_GAP)
print(f"low-rate phase: {n} offers every {LOW_GAP}s over {n*LOW_GAP:.1f}s into a healthy child: congested={i._tx_congested} drops_in_phase={i.tx_dropped-d0} occ={occ()} qsize={i._txq.qsize()}")
# burst: 2000 x 500 B tight from a worker (the healthy-burst test's shape at the RNS MTU)
d1 = i.tx_dropped; worst = [0.0]
def work():
    for _ in range(2000):
        t = time.monotonic(); i.process_outgoing(b"b" * 500); worst[0] = max(worst[0], time.monotonic() - t)
th = threading.Thread(target=work, daemon=True); tb = time.monotonic(); th.start(); th.join(20)
print(f"burst: drops={i.tx_dropped-d1} of 2000 worst_call_ms={worst[0]*1000:.1f} took={time.monotonic()-tb:.2f}s congested_after={i._tx_congested}")
time.sleep(1.0)
print(f"RESULT mode={mode} LOW_GAP={LOW_GAP} congested_after_low={'see above'} burst_drops={i.tx_dropped-d1} total_dropped={i.tx_dropped}")
i.detached = True; i.respawn_delay = 3600; i.process.kill()
