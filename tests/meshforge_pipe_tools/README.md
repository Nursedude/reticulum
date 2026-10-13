# PipeInterface gate tools (MeshForge fork)

Reviewer/author tools for `RNS/Interfaces/PipeInterface.py`'s congestion gate.
No RNS instance is started; the fixture is `tests/meshforge_pipe_interface_stall.py`'s.

- `drill_occ.py <tree> fast|gulp|slowgulp|burst [seconds]` — per put that waited, what the
  kernel says: `select()` writability (the mf.7 signal) and FIONREAD occupancy
  (the mf.8 signal), bucketed per 100 ms. Run it on BOTH kernel classes: the
  fleet's Pi 3/4 (4 KB pages, 64 KB pipe) and Pi 5 (16 KB, 256 KB).
  Scenarios: `fast` = 500-B frames at 2400 f/s into an 800 kB/s child;
  `gulp` = 1000 B at 600 f/s into a child that reads up to 1 MB at a time at a
  200 kB/s mean; `slowgulp` = the same gulp child offered 500 B at 600 f/s
  (300 kB/s — the band where, on 16 KB pages, the queue dump fits the emptied
  pipe); `burst` = 2000 x 1000 B tight into a healthy child.
- `drill_pulse_x.py <tree>` (mf.9, F1) — a healthy 2 MB/s child fed pulses of
  4 x 100 x 1000 B every `PERIOD` s (env `PERIOD`, `RATE`, `NP`; `LOG=1` prints
  a state line before and after each pulse: occupancy, qsize, span start,
  full/room time). A drained pipe (`occ=0 qsize=0`) must show no span open,
  and a healthy child must never enter. Run it >= 10x for a RATE: on 9120f760
  1 of 5 runs entered. Controls: `PERIOD=0.6` (quiet longer than the span),
  `RATE=100000` (a child that IS behind must enter).
- `drill_tick.py <tree> tick_child.py` (mf.9, F4) — a child that reads
  everything every 0.25 s, provoked at `PROV_BPS` (400 kB/s) for 3 s, then
  held at `RATE_BPS` for `P2_S` (10 s); per-second lines with drops,
  offers, offers seeing the pipe >= 50 %, peak occupancy. At 160 kB/s on a
  64 KB pipe the child keeps up while each tick peaks above the 32 KB line;
  the episode must close and drops go flat. Control: `RATE_BPS=100000`.
- `drill_burst.py <tree> [ready]` — the healthy-burst test's exact shape with
  every `_note_pipe_full` call and every drop recorded; `ready` waits for the
  child's first read first (the mf.7 "1 drop" was frame 321 = 64 + 256 + 1).
- `mutants8.sh <tree> <outdir> [python]` — pattern-anchored mutant matrix over
  the whole stall file; `MUTS="a b"` selects a subset. Re-run the WHOLE matrix
  after every fix (a fix can unpin an older leg), on both kernel classes. A
  mutant whose pattern no longer matches reports `SED DID NOT APPLY` — re-anchor
  it, never drop it. mf.9: 29 mutants (the mf.8 author's 11, the 9120f760
  pass's 6, 12 aimed at the mf.9 changes); ~2.5 min each on a Pi 4.

First step of any pass: `getconf PAGESIZE` on every box the build ships to.
