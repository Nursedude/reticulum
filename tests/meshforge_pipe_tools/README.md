# PipeInterface gate tools (MeshForge fork)

Reviewer/author tools for `RNS/Interfaces/PipeInterface.py`'s congestion gate.
No RNS instance is started; the fixture is `tests/meshforge_pipe_interface_stall.py`'s.

- `drill_occ.py <tree> fast|gulp|burst [seconds]` — per put that waited, what the
  kernel says: `select()` writability (the mf.7 signal) and FIONREAD occupancy
  (the mf.8 signal), bucketed per 100 ms. Run it on BOTH kernel classes: the
  fleet's Pi 3/4 (4 KB pages, 64 KB pipe) and Pi 5 (16 KB, 256 KB).
- `drill_burst.py <tree> [ready]` — the healthy-burst test's exact shape with
  every `_note_pipe_full` call and every drop recorded; `ready` waits for the
  child's first read first (the mf.7 "1 drop" was frame 321 = 64 + 256 + 1).
- `mutants8.sh <tree> <outdir> [python]` — pattern-anchored mutant matrix over
  the whole stall file; `MUTS="a b"` selects a subset. Re-run the WHOLE matrix
  after every fix (a fix can unpin an older leg), on both kernel classes.

First step of any pass: `getconf PAGESIZE` on every box the build ships to.
