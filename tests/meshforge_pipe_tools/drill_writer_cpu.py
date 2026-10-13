# Writer-thread CPU over a window (default 10 s). Adapted from the mf.9 non-author
# pass's idlecpu.py. Usage: drill_writer_cpu.py <tree> <mode> [window_s]
#   idle      — never congested, nothing offered.
#   quietexit — SIGSTOP-provoke an episode, SIGCONT, then nothing offered: the
#               writer's polls must close the episode, then fall back to 0.5 s.
#   hung      — SIGSTOP-provoke an episode and keep the child stopped (pipe full:
#               the writer is wedged in a write, so it cannot poll).
#   hungpoll  — the worst case for the poll: child stopped, pipe ~40 KB (over the
#               32 KB line on a 64 KB pipe, under full), queue empty, the episode
#               flag set by hand — the writer polls a pipe that never changes.
import os, sys, time, tempfile, signal, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]; mode = sys.argv[2]; W = float(sys.argv[3]) if len(sys.argv) > 3 else 10
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "cpu", "command": f"{sys.executable} -u {child}"})
fd = i.process.stdin.fileno()
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
time.sleep(0.5)
calls = [0]; orig = PI.PipeInterface._child_caught_up
def spy(self): calls[0] += 1; return orig(self)
i._child_caught_up = spy.__get__(i)
if mode in ("quietexit", "hung"):
    os.kill(i.process.pid, signal.SIGSTOP)
    te = time.monotonic() + 1.5
    while time.monotonic() < te and not i._tx_congested: i.process_outgoing(b"s" * 500)
    if mode == "quietexit": os.kill(i.process.pid, signal.SIGCONT)
    time.sleep(0.2)
elif mode == "hungpoll":
    os.kill(i.process.pid, signal.SIGSTOP)
    for _ in range(80): i.process_outgoing(b"s" * 500)   # ~40 KB
    time.sleep(0.5)
    i._tx_congested = True
tid = i._writer.native_id; hz = os.sysconf("SC_CLK_TCK")
def ticks():
    f = open(f"/proc/{os.getpid()}/task/{tid}/stat").read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12])
def vcs():
    for l in open(f"/proc/{os.getpid()}/task/{tid}/status"):
        if l.startswith("voluntary_ctxt_switches"): return int(l.split()[1])
print(f"mode={mode} start: occ={occ()} qsize={i._txq.qsize()} span_open={i._tx_full_since is not None} congested={i._tx_congested}")
c0 = calls[0]; a = ticks(); v0 = vcs(); t = time.monotonic()
time.sleep(W)
dt = time.monotonic() - t; da = ticks() - a; dv = vcs() - v0
print(f"RESULT mode={mode} window={dt:.1f}s writer_cpu={da/hz:.3f}s ({100*da/hz/dt:.2f}% of a core) writer_wakeups={dv} ({dv/dt:.1f}/s) caught_up_checks={calls[0]-c0} congested_end={i._tx_congested} span_open_end={i._tx_full_since is not None} occ_end={occ()}")
try: os.kill(i.process.pid, signal.SIGCONT)
except Exception: pass
i.detached = True; i.respawn_delay = 3600; i.process.kill()
