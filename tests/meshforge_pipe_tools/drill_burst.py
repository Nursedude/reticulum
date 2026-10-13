"""Where does the healthy-child burst's single drop come from on a 4 KB box?
Runs the test's exact shape (2000 x 1000 B from a worker thread, LOG_DEBUG) and
records every call into _note_pipe_full (waited puts AND timeouts) with the
put's wait, queue size, select state and occupancy, plus every drop."""
import os, sys, time, fcntl, termios, struct, select, tempfile, threading
from unittest import mock
tree = sys.argv[1]
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_DEBUG
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
patches = [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
           mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
           mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]
for p in patches: p.start()
rec = []
class Probe(PI.PipeInterface):
    def _note_pipe_full(self, now, *a):
        fd = self.process.stdin.fileno()
        w = bool(select.select([], [fd], [], 0)[1]); o = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        rec.append(("note", now, self._txq.qsize(), w, o, self.tx_dropped))
        return super()._note_pipe_full(now, *a)   # mf.9: (now, waited)
for run in range(3):
    iface = Probe(T.FakeOwner(), {"name": "burst", "command": f"{sys.executable} -u {child}"})
    cap = fcntl.fcntl(iface.process.stdin.fileno(), 1032)
    rec.clear(); worst = [0.0]; drops_at = []
    def work():
        for i in range(2000):
            t = time.monotonic(); d0 = iface.tx_dropped
            iface.process_outgoing(b"x" * 1000)
            dt = time.monotonic() - t
            worst[0] = max(worst[0], dt)
            if iface.tx_dropped != d0: drops_at.append((i, round(dt * 1000, 1)))
    # readiness: one frame, wait until the child has READ it (pipe empty, txb > 0)
    if len(sys.argv) > 2 and sys.argv[2] == "ready":
        iface.process_outgoing(b"r" * 1000); tr = time.monotonic()
        while time.monotonic() - tr < 5 and (iface.txb == 0 or struct.unpack("i", fcntl.ioctl(iface.process.stdin.fileno(), termios.FIONREAD, b"\0\0\0\0"))[0] > 0): time.sleep(0.002)
        print(f"   child ready after {time.monotonic() - tr:.3f}s")
    t0 = time.monotonic(); th = threading.Thread(target=work, daemon=True); th.start(); th.join(20.0)
    el = time.monotonic() - t0; time.sleep(0.5)
    notes = [r for r in rec if r[0] == "note"]
    print(f"run{run}: cap={cap} elapsed={el:.2f}s worst_put={worst[0]*1000:.1f}ms dropped={iface.tx_dropped} drops_at(frame#,put_ms)={drops_at} congested_ever={any(True for r in notes if False) or iface.tx_congested}")
    print(f"   _note_pipe_full calls={len(notes)}; at those: select_writable={sum(1 for r in notes if r[3])}/{len(notes)} occ>=50%={sum(1 for r in notes if r[4] >= cap/2)}/{len(notes)} qsize_median={sorted(r[2] for r in notes)[len(notes)//2] if notes else None}")
    iface.detached = True
    try: iface.process.kill()
    except Exception: pass
    iface._writer.join(2)
for p in patches: p.stop()
