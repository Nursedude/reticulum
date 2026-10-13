# F4 drill from the non-author pass on 9120f760 (2026-10-12). Usage: drill_tick.py <tree> <tick_child.py>.
# F4: tick child. Phase 1: PROV_BPS for 3 s (provoke); phase 2: RATE_BPS for 10 s. 1000-B frames, one steady sender thread.
import os, sys, time, tempfile, fcntl, termios, struct
from unittest import mock
tree = sys.argv[1]; childpath = sys.argv[2]
PROV = float(os.environ.get("PROV_BPS", "400000")); RATE = float(os.environ.get("RATE_BPS", "160000")); P2 = float(os.environ.get("P2_S", "10"))
sys.path.insert(0, tree); sys.path.insert(0, os.path.join(tree, "tests"))
import RNS, RNS.Interfaces.PipeInterface as PI, meshforge_pipe_interface_stall as T
RNS.loglevel = RNS.LOG_WARNING
tmp = tempfile.TemporaryDirectory()
for p in [mock.patch.object(RNS.Reticulum, "transport_enabled", staticmethod(lambda: False)),
          mock.patch.object(RNS.Reticulum, "get_instance", staticmethod(lambda: mock.MagicMock(interfacepath=tmp.name))),
          mock.patch.object(RNS.Reticulum, "panic_on_interface_error", False, create=True)]: p.start()
i = PI.PipeInterface(T.FakeOwner(), {"name": "tick", "command": f"{sys.executable} -u {childpath}"})
time.sleep(0.5)
fd = i.process.stdin.fileno(); cap = fcntl.fcntl(fd, 1032)
def occ(): return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
print(f"PROV={PROV} RATE={RATE} P2={P2} cap={cap}")
t0 = time.monotonic(); ev = []
for ph, dur, bps in (("prov", 3.0, PROV), ("main", P2, RATE)):
    fs = bps / 1000.0; ts = time.monotonic(); te = ts + dur; n = 0; sec = int(ts - t0); hi = 0; offers = 0; maxo = 0
    while time.monotonic() < te:
        n += 1; i.process_outgoing(b"t" * 1000); o = occ(); offers += 1; maxo = max(maxo, o)
        if o >= cap * 0.5: hi += 1
        s = int(time.monotonic() - t0)
        if s != sec:
            print(f"  [{ph}] sec={sec} congested={i._tx_congested} dropped={i.tx_dropped} offers={offers} occ>=50%={hi} max_occ={maxo} clear_since={'None' if i._tx_clear_since is None else round(i._tx_clear_since-t0,2)}")
            sec = s; hi = 0; offers = 0; maxo = 0
        rem = ts + n / fs - time.monotonic()
        if rem > 0: time.sleep(rem)
print(f"tick: congested_at_end={i._tx_congested} dropped={i.tx_dropped} probe_errors={i.tx_probe_errors} txb={i.txb}")
i.detached = True; i.process.kill()
