"""Measure, per put that waited, what the kernel says about the pipe: select
writability (the mf.7 signal) and FIONREAD occupancy (bytes). Scenarios: a
child behind at 800 kB/s (500 B @ 2400 f/s), a gulp child (1000 B @ 600 f/s),
a healthy child under a 2000-frame burst. No RNS instance; the fixture is the
fork test file's. Usage: drill_occ.py <tree> <scenario> [seconds]"""
import os, sys, time, fcntl, termios, struct, select, tempfile, threading
from unittest import mock
tree = sys.argv[1]; scen = sys.argv[2]; secs = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS
import RNS.Interfaces.PipeInterface as PI
import meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py")
open(child, "w").write(T.CHILD)
patches = [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
           mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
           mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]
for p in patches: p.start()
cmd = {"fast": " rate 800000", "gulp": " gulp 200000", "burst": "", "slowgulp": " gulp 200000"}[scen]
iface = PI.PipeInterface(T.FakeOwner(), {"name": scen, "command": f"{sys.executable} -u {child}{cmd}"})
fd = iface.process.stdin.fileno(); cap = fcntl.fcntl(fd, 1032)
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
samples = []   # (t, waited_s, qsize_before, writable, occ)
orig_note = iface._note_pipe_full
entered = []
class Spy(type(iface)):
    @property
    def _tx_congested(self): return self.__dict__.get("_c", False)
    @_tx_congested.setter
    def _tx_congested(self, v):
        if v and not self.__dict__.get("_c", False): entered.append(time.monotonic())
        self.__dict__["_c"] = v
iface.__class__ = Spy
size, rate = {"fast": (500, 2400.0), "gulp": (1000, 600.0), "slowgulp": (500, 600.0), "burst": (1000, None)}[scen]
n = 2000 if scen == "burst" else None
t0 = time.monotonic(); k = 0; waits = 0; busy = 0.0
while (k < n) if n else (time.monotonic() - t0 < secs):
    k += 1
    q = iface._txq.qsize(); full_at_arrival = iface._txq.full()
    t = time.monotonic(); iface.process_outgoing(b"x" * size); dt = time.monotonic() - t
    busy += dt
    if full_at_arrival or dt >= 0.001:
        w = bool(select.select([], [fd], [], 0)[1]); o = occ()
        samples.append((t - t0, dt, q, w, o, iface._tx_congested))
        if dt >= 0.015: waits += 1
    if rate:
        rem = t0 + k / rate - time.monotonic()
        if rem > 0: time.sleep(rem)
elapsed = time.monotonic() - t0
time.sleep(0.6)
print(f"scenario={scen} pagesize={os.sysconf('SC_PAGESIZE')} cap={cap} py={sys.version.split()[0]}")
print(f"offered={k} elapsed={elapsed:.2f}s busy={busy:.2f}s waits(>=15ms)={waits} dropped={iface.tx_dropped} entered={len(entered)} at={[round(e - t0, 2) for e in entered]}")
if samples:
    import statistics as st
    occs = [s[4] / cap for s in samples]; wr = [s[3] for s in samples]; dts = [s[1] for s in samples]
    hi = sum(1 for o in occs if o >= 0.5)
    print(f"samples(put waited or queue full)={len(samples)} median_wait={st.median(dts)*1000:.1f}ms p90={sorted(dts)[int(len(dts)*0.9)]*1000:.1f}ms")
    print(f"  select writable at sample: {sum(wr)}/{len(wr)} = {sum(wr)/len(wr):.2f}   occupancy>=50%: {hi}/{len(occs)} = {hi/len(occs):.2f}   median occ={st.median(occs):.2f} min={min(occs):.2f}")
    # 100 ms buckets: fraction writable / fraction occ>=0.5 over time
    buckets = {}
    for s in samples:
        b = int(s[0] * 10); buckets.setdefault(b, []).append(s)
    row = []
    for b in sorted(buckets)[:60]:
        ss = buckets[b]; row.append(f"{b/10:.1f}:{sum(1 for x in ss if x[3])}/{len(ss)}w,{sum(1 for x in ss if x[4]/cap>=0.5)}/{len(ss)}o")
    print("  per-100ms [t:writable/samples w, occ>=50%/samples o]:", " ".join(row))
iface.detached = True
try: iface.process.kill()
except Exception: pass
for p in patches: p.stop()
