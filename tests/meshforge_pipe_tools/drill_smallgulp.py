# SOURCE: copied from the mf.10 non-author pass's reviewer drill `smallgulp.py`
# (fresh pass on 8acd4d18, moc4, 2026-10-12; md5 8e1b7c5ad791 of the original),
# used by accept.sh leg 11 (F3 frame size). Adaptation: FPS may be `auto`,
# which holds the offered MEAN bytes/s at OFFERED_BPS (default 40000, the
# original's 30 B x 1250 f/s) whatever SIZE is, so SIZE is the only axis moved.
# Reviewer drill (mf.10 non-author pass): a child BEHIND that reads in gulps,
# offered SMALL frames, so a full queue (256 frames) fits under the 50 % line
# of a 64 KB pipe. After each gulp the writer dumps the queue, the queue reads
# empty and the pipe reads under the line until the offered stream refills it
# -- for (cap/2 - 256*frame) / offered_rate seconds. If that exceeds half a
# TX_CONGEST_FULL_S window the exit rule closes the episode about a child that
# is still behind, and the caller pays a fresh entry span per gulp cycle.
# Usage: smallgulp.py <tree>   env SIZE (payload B, default 30), FPS (default
#   1250), GULP (child mean B/s, default 30000), SECS (default 12), CHILDMODE
#   (gulp|rate, default gulp -- `rate` is the CONTROL: same mean, no gulps)
import os, sys, time, tempfile, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]
SIZE = int(os.environ.get("SIZE", "30")); _f = os.environ.get("FPS", "1250")
FPS = float(os.environ.get("OFFERED_BPS", "40000")) / (SIZE + 2) if _f == "auto" else float(_f)
GULP = float(os.environ.get("GULP", "30000")); SECS = float(os.environ.get("SECS", "12"))
CM = os.environ.get("CHILDMODE", "gulp")
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory(); child = os.path.join(tmp.name, "child.py"); open(child, "w").write(T.CHILD)
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "sg", "command": f"{sys.executable} -u {child} {CM} {int(GULP)}"})
fd = i.process.stdin.fileno(); cap = fcntl.fcntl(fd, 1032)
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
trans = []
class Spy(type(i)):
    @property
    def _tx_congested(self): return self.__dict__.get("_c", False)
    @_tx_congested.setter
    def _tx_congested(self, v):
        if bool(v) != self.__dict__.get("_c", False): trans.append((time.monotonic(), bool(v)))
        self.__dict__["_c"] = v
i.__class__ = Spy
time.sleep(0.5)
t0 = time.monotonic(); k = 0; waits = 0; busy = 0.0
while time.monotonic() - t0 < SECS:
    k += 1
    t = time.monotonic(); i.process_outgoing(b"g" * SIZE); dt = time.monotonic() - t
    busy += dt
    if dt >= 0.015: waits += 1
    rem = t0 + k / FPS - time.monotonic()
    if rem > 0: time.sleep(rem)
el = time.monotonic() - t0
ent = [round(t - t0, 2) for t, v in trans if v]; ex = [round(t - t0, 2) for t, v in trans if not v]
offered_bps = k * (SIZE + 2) / el
print(f"cap={cap} frame={SIZE+2}B queue_bytes_full={256*(SIZE+2)} offered={offered_bps:.0f}B/s child_mean={GULP:.0f}B/s ({CM}) txb={i.txb} delivered_bps={i.txb/el:.0f}")
print(f"RESULT SIZE={SIZE} FPS={FPS} GULP={GULP} CM={CM} entries={len(ent)} exits={len(ex)} entered_at={ent} exited_at={ex} waits={waits} caller_busy={busy:.2f}s duty={busy/el:.3f} dropped={i.tx_dropped} offered={k} congested_end={i._tx_congested}")
i.detached = True; i.respawn_delay = 3600; i.process.kill()
