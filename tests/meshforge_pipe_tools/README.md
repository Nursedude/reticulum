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
- `accept.sh <tree> <outdir> [python]` (mf.10) — the ACCEPTANCE run: every
  known traffic shape at once, with its controls, sequentially, waiting for
  loadavg < 1.0 before each run; rc per run to files, then a one-line-per-leg
  verdict table (`accept_verdict.py`, exit 0 all GREEN / 1 a RED / 2 UNKNOWN).
  The healthy and defective reading of each leg is in the script's header.
  Drills come from this directory, the gate from `<tree>`, so it measures any
  cut: run it on the parent BEFORE a change (the red baseline) and on the
  change AFTER it. Legs: pulse x5 (+ behind control), tick 160k (+ 100k),
  sticky 0.7 s x3 (+ 0.3 s, no episode), quiet-then-burst x3 (+ no episode),
  slowgulp, hung (SIGSTOP), healthy burst, behind (`rate 100000`). ~4 min.
  `<outdir>` may be relative (made absolute before the runs `cd` into the tree;
  a relative one broke every run before 2026-10-12).
  Legs 9-11 (2026-10-12, after the fresh pass on 8acd4d18, arc paused) move
  three AXES instead of adding shapes: burner count B (pure-Python spin
  threads, 0 or 2, started before the provoke and kept through the burst),
  idle duration (3 s and `TX_STALL_S + 1`, read from the class), and frame
  SIZE for the gulp leg (30 B and 500 B). 9 = sticky under B=2 (+ B=0, +
  no-episode B=2); 10 = idle `TX_STALL_S + 1` then burst at B=2 x5, asserting
  no `has not drained its stdin` line and `online` never False (+ B=0 x3, +
  3 s idle B=2 x3); 11 = gulp child at SIZE=30 (+ `rate` child same mean, +
  SIZE=500). `LEGS="9 10 11"` selects legs; the verdict judges only those.
  LOAD rule: every B=2 run reports the writer's `poll_gap_median_ms` (only
  trees with `_writer_observes`, only while flagged; preferred when >= 5 polls)
  and `wake_gap_median_ms` (the main thread waiting `queue.get(timeout=
  TX_PUT_WAIT_S)` through the quiet — tree-independent, but it under-reads the
  writer by up to 8x on moc4). A HEALTHY B=2 run under 100 ms is UNKNOWN
  (contention not reached), never GREEN; a DEFECTIVE one stays RED; an episode
  run that never entered is UNKNOWN; a leg with any RED run is RED. Leg 9 uses
  `PROVOKE_S=4` (burners start before the provoke, which slows entry). Legs
  9-11 add ~9 min on a Pi 4. The index's first line says WHERE it ran (PAGESIZE,
  F_GETPIPE_SZ, python, nproc, loadavg). A leg GREEN on every tree measured
  nothing — the red baseline is what makes it a leg.
- `drill_gil.py <tree> episode|control` (legs 9, 10) — the fresh pass's
  `gilsticky.py`, copied with a source header; env `BURNERS`, `HOGS`,
  `QUIET_S` (`stall+1` allowed), `LOW_GAP`, `BURST_WITH_AUX`,
  `AUX_BEFORE_PROVOKE` (1 default; 0 = the reviewer's order), `PROVOKE_S`
  (1.5 default). RESULT adds
  `wake_gap_median_ms`, `offline_events`, `stall_lines`.
- `drill_smallgulp.py <tree>` (leg 11) — the fresh pass's `smallgulp.py`,
  copied; env `SIZE`, `FPS` (`auto` holds `OFFERED_BPS`, default 40000),
  `GULP`, `SECS`, `CHILDMODE` (gulp|rate).
- `drill_sticky.py <tree> episode|control` — the mf.9 non-author pass's drill,
  copied: episode, then offers `LOW_GAP` (0.7) s apart for `LOW_S` (10) s,
  then 2,000 x 500 B. On c3c52232 the flag stuck and the burst dropped
  1,557-1,744 of 2,000.
- `drill_shapes.py <tree> quietburst|quietctl|hung|behind` (mf.10) — episode
  then NO offers for `QUIET_S` then the burst; a SIGSTOPped child held
  `HOLD_S` under 1000 f/s; a `rate 100000` child under 300 kB/s. Prints one
  `RESULT` line each.
- `drill_writer_cpu.py <tree> idle|quietexit|hung|hungpoll [s]` (mf.10) — the
  writer thread's CPU seconds and voluntary wakeups over a window, from
  `/proc/<pid>/task/<tid>`; `hungpoll` is the poll's worst case (congested,
  queue empty, pipe over the line and never changing).
- `mutants8.sh <tree> <outdir> [python]` — pattern-anchored mutant matrix over
  the whole stall file; `MUTS="a b"` selects a subset, `FAILFAST=1` stops each
  run at its first failure. Re-run the WHOLE matrix after every fix (a fix can
  unpin an older leg), on both kernel classes. A mutant whose pattern no longer
  matches reports `SED DID NOT APPLY` — re-anchor it, never drop it. mf.10: 40
  mutants (mf.9's 29, the c3c52232 pass's 5, 6 aimed at the mf.10 exit
  accounting); ~3 min each on a Pi 4 for a survivor, less when FAILFAST kills.

First step of any pass: `getconf PAGESIZE` on every box the build ships to.
