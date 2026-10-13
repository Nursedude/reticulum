# MeshForge fork of Reticulum (RNS)

This is a maintained fork of [Reticulum](https://github.com/markqvist/Reticulum)
by Mark Qvist, owned by the MeshForge project (Nursedude).

## Base

- **Upstream:** `markqvist/Reticulum`
- **Forked at tag:** `1.2.5` (original vendoring anchor)
- **Current base tag:** `1.3.8` (merged 2026-07-17; see Upstream merge history)
- **Fork branch:** `meshforge`
- **Version scheme:** PEP 440 local marker on the upstream base —
  `<base>+mf.0`, `<base>+mf.1`, … The base version is never changed
  independently of upstream; only the `+mf.N` segment increments for
  MeshForge changes, and it resets to `+mf.0` when the base is bumped to a
  new upstream tag.

## Why this fork exists

RNS upstream executed a "Carrier Switch" in December 2025: public support
withdrawn, issue tracker hidden, repository a read-only mirror, single author,
no support or security-disclosure channel. Code still ships, but unilaterally
and unreviewed. MeshForge depends on RNS for its Meshtastic↔Reticulum gateway
and has hit a recurring *rnsd-RPC fragility* class (a wedged daemon hanging
clients). We fork to **own the dependency**: a version bump is a reviewed
decision, and fragility can be fixed at the source instead of worked around.

`1.2.5` is the last GitHub-published RNS release and the pair field-proven on
the MeshForge fleet (with LXMF `0.9.4`), so this is the natural, verified
vendoring anchor — not a leap to untested latest.

## Hard invariant — DO NOT VIOLATE

**Never change the cryptographic primitives (Ed25519 / X25519 / AES-256-CBC /
Fernet) or the packet / announce / path-table wire format.** Changing the wire
format forks the *network*, not just the code — it breaks interoperability with
stock RNS, NomadNet, and Sideband on the public Reticulum network, which
MeshForge depends on.

The fork's job is **maintenance and isolation, not redesign.** Changes are
limited to: reliability fixes that do not alter the wire (e.g. bounding socket
connects / RPC round-trips so a wedged daemon can't hang a thread), packaging,
and provenance metadata.

## License

This fork retains the upstream **Reticulum License** (see `LICENSE`) — a
permissive MIT-derivative that grants use/copy/modify/distribute, with added
"no-harm" and "no-AI/ML-training-dataset" use restrictions. MeshForge's use
(comms infrastructure; inference via an external API, not training-set
creation) does not implicate those restrictions. The upstream copyright and
permission notice are preserved. MeshForge modifications are recorded in
`NOTICE`.

## Tracking upstream

Stock RNS may publish future releases off-GitHub. To incorporate one:

1. Add the upstream as a remote and fetch the new tag.
2. `git merge <upstream-tag>` into `meshforge` (our `+mf.N` patches are a clean
   series on top of the base tag).
3. Re-run the MeshForge Phase-1 parity verification (see
   `.claude/plans/` in the MeshForge repo): version marker, `rnsd` ownership,
   gateway/map/tracer, and a **public-net interop proof** (receive a stock-node
   announce + round-trip an LXMF message to NomadNet/Sideband).
4. Canary on one box before rolling to the fleet.

Bump the base version to match upstream and reset the local marker to `+mf.0`
for the new base.

## Upstream merge history

### `1.2.5+mf.5` → `1.3.8+mf.0` (2026-07-17)

Adopted upstream `1.3.8`. Wire-compat invariant cleared (crypto primitives
untouched; the one big transport change — the shared-instance RPC rewrite to
msgpack byte-mode — is LOCAL client↔rnsd IPC, not the network wire). Conflicts
were confined to `RNS/Reticulum.py` (20 RPC-callsite hunks) and `_version.py`.

Two reconciliation lessons, both load-bearing for future merges:

- **`#72` is not subsumed.** Upstream's `get_rpc_client()` is still a bare
  `Client()` with no timeout and a raw blocking `recv_bytes()`, so a wedged
  rnsd still hangs. The bounded `_rpc_recv` (poll-then-recv) was re-ported onto
  the new msgpack framing and all 21 client recv sites route through it —
  keeping upstream's per-site try/except AND the wedge bound. Do not drop this
  on a future merge just because upstream "added error handling."
- **`mf.4` was re-ported, not carried verbatim.** The original `mf.4` made
  `logging_lock` an `RLock` so `log()`'s on-write-failure fallback (which
  re-calls `log()` under the lock) wouldn't self-deadlock. Live re-validation on
  1.3.8's link/resource suite showed the RLock's per-acquire overhead on the hot
  logging path flaked LOG_EXTREME resource transfers (controlled A/B on clean
  1.3.8: plain `Lock` 9/9 pass, `RLock` 2/5 fail). Cured structurally instead —
  keep a plain `Lock`, run the fallback re-log *after* releasing it. This is the
  general rule: prefer the narrowest fix, and re-validate a carried patch against
  the new base rather than assuming a textual auto-merge preserved its behavior.

Not fleet-rolled at merge time: the `meshforge` branch stays at `1.2.5+mf.5`
until a one-box canary + wedge-probe/clean-stop soak + public-net interop proof
pass. The msgpack RPC rewrite is box-local, so a box's client and rnsd must be
upgraded together (coordinated per-box, never rapid-cycle).

## MeshForge patch history on the `1.3.8` base

### `1.3.8+mf.10` (UNRELEASED) — the congestion gate's exit uses BOTH of its observers, so quiet that the writer observed drained closes an episode, and an offer gap is no longer a reset

Built red-first ON a 4 KB-page box (moc4, Raspberry Pi 4, Python 3.11.2,
`F_GETPIPE_SZ` 65536 → the 50 % line is 32 KB). This is an AUTHOR's cut: it
carries no non-author pass and is not tagged; `RNS/_version.py` is not bumped
(it still reads mf.7 — the bump belongs to the release commit). The spec is the
fresh non-author pass on `c3c52232` (the mf.9 candidate), which found it NOT
tag-ready with one defect mf.9 introduced, confirmed live 3/3 with an A/B.

**The pattern this cut was built against.** Three cuts, three strangers, three
reds — and each red was a defect the PREVIOUS cut's own tests pinned as
correct: mf.7's `anyroom`, mf.9's `test_a_quiet_gap_between_offers_restarts_
the_exit_window` (the single test killing `quietexit`/`stickyhold`). Each cut
cured the SHAPE the last pass found. mf.10 is built from a stated principle
and accepted against EVERY known shape at once (`accept.sh`, below), before
and after the change, on the parent, on itself, and on mf.8.

**The principle.** The gate credits only time some observer actually
OBSERVED, and it uses every observer it has. It has two:

1. **An offer** — one instant, worth at most `TX_PUT_WAIT_S` of the time
   around it (mf.9 got this right on entry and on exit).
2. **The writer** — while a pipe-full span is open OR the interface is
   congested, its idle queue wait is `TX_PUT_WAIT_S` (else 0.5 s), and every
   timed-out wait is a poll: queue empty by construction, pipe read by FIONREAD.
   During quiet between offers the writer is the ONLY observer, and what it
   sees — queue empty AND pipe under the line — is observed caught-up time.

**What the c3c52232 pass measured (moc4, `~/mf9-review/out/drills/`).**
`PipeInterface.py:444` restarted the exit window at any offer more than
`TX_CONGEST_FULL_S` after the previous one. Under sparse traffic (< 2 frames/s)
every offer restarted it, so an episode NEVER closed; while stuck there is no
back-pressure, so the next burst into a perfectly healthy child was dropped.
`sticky.py` (SIGSTOP-provoke → SIGCONT → 14 offers 0.7 s apart → 2,000 × 500 B):
`congested=True` after 9.8 s of healthy traffic (occ=0, qsize=0, 0 drops), then
`drops=1557/1741/1739 of 2000`; `9120f760` 0 and 0; no-episode control 0/0/0;
0.3 s gaps exited, 0 drops. Lesser: `_child_caught_up()` read outside
`_tx_gate_lock` before `_end_span`, `configure_pipe` reset gate state unlocked
(PLAUSIBLE, not reproduced); `tx_probe_errors` cumulative across children reads
"degraded" in rnstatus forever after one transient ioctl error; the mutant
`waitfloor20` (the `TX_PUT_WAIT_S / 2` sampling floor) survived.

What changed (`RNS/Interfaces/PipeInterface.py`, `RNS/Utilities/rnstatus.py`):

1. **One exit accounting for both observers** (`_caught_up_for(now,
   caught_up)`, under `_tx_gate_lock`). Each observation — an offer while
   congested, or a writer poll (`_writer_observes`) — credits `min(time since
   the previous observation by EITHER observer, TX_PUT_WAIT_S)` to caught-up
   (queue empty and pipe under the line) or not. The exact rule:
   - a frame dropped since the previous observation, or the queue over
     `TX_LOW_WATER`, ends the window (what the domain loses) — unchanged;
   - the first observation after that opens a window and credits nothing;
   - when the window is `TX_CONGEST_FULL_S` old the episode closes if
     observed caught-up time **exceeds** observed backed-up time **and covers
     at least half the window** (`TX_CONGEST_FULL_S / 2`); otherwise the next
     window starts at that observation;
   - **an offer GAP is not evidence either way and is no longer a reset** —
     the writer's polls fill it. Time nobody observed credits nothing, and a
     window observed for less than half its length cannot close (that is what
     the gap reset was trying to say, stated in observed time instead of in
     offers). With the queue empty and the pipe under the line for
     `TX_CONGEST_FULL_S` of polls, the episode closes whether or not anyone
     offered: quiet + drained, observed, IS caught up.
   - A child genuinely behind stays congested by the same rule: its polls (if
     the writer is idle at all) and its offers see the pipe over the line, or
     the queue over low water, or drops.
2. **The writer polls while congested**, not only while a span is open
   (`watching = span open or congested`), and closes the episode itself
   (`_end_episode`) when its observation completes a window. It observes only
   while `online`: a dead child's pipe before its respawn, or a stalled one,
   is not a child catching up (and must not log "caught up" about it).
3. **Poll cadence and cost — decision: no back-off.** `TX_PUT_WAIT_S` while
   watching, 0.5 s otherwise (so it already falls back once an episode has
   closed). Measured on moc4 (`drill_writer_cpu.py`, writer thread
   `/proc/<pid>/task/<tid>`, 10 s windows, CLK_TCK 100):

   | state | writer CPU / 10 s | wakeups/s |
   |---|---|---|
   | idle, never congested | 0.000 s | 1.9 |
   | congested, child resumed, nothing offered (the polls close it, then 0.5 s) | 0.010 s (0.10 %) | 3.7 (18 caught-up checks) |
   | hung child (SIGSTOP held; pipe full, writer wedged in a write) | 0.000 s | 0.0 |
   | worst case: congested, child stopped with the pipe at 40 KB (over the line, under full), queue empty — polls a pipe that never changes | 0.080 s (0.80 %) | 49.4 |

   The worst case costs twice mf.9's 0.4 % (each poll now runs the exit
   accounting under the lock as well as the span check), at 8 ticks of
   resolution, and only arises for a child stopped with its pipe between half
   and full and nothing more offered — a stopped child that is offered to
   fills its pipe and wedges the writer (0 wakeups). A back-off would make the
   observer the exit depends on sparser exactly while it is the only one;
   bounded at < 1 % of a Pi 4 core, that trade was not worth a new state.
4. **Locks** (both fixed, not justified): `_end_span(observed_at)` clears a
   span only if it opened at or before the writer's caught-up read (taken
   before the ioctl, outside the lock); a span a sender opened after that read
   survives. `configure_pipe` resets the gate's state under `_tx_gate_lock`.
5. **`tx_probe_errors` — decision: a lifetime count, worded as one.** Not reset
   per child (a respawn would erase the only witness of a probe that fails on
   every child). rnstatus now reads `Pipe probe: N failed read(s) of the stdin
   pipe state since the interface started (a running total, not the current
   state)`. Not rendered against a live rnsd (tested on the source line).

**Tests (`tests/meshforge_pipe_interface_stall.py`, 49 → 57).** REMOVED:
`test_a_quiet_gap_between_offers_restarts_the_exit_window` — it pinned the
defect. Red-first — the final 57-test file run against the `c3c52232` gate on
moc4 (`tree_c3red` = this tree with `PipeInterface.py` and `rnstatus.py` from
`c3c52232`, gate md5 `2c25c69ed3c9`, identical to the pass's tree): `Ran 57 …
FAILED (failures=7)`, rc=1 (and the same 7 on the 56-test draft before the
offline pin was added):

| test | on `c3c52232` (moc4) |
|---|---|
| (i) `test_writer_polls_that_see_the_child_caught_up_close_the_episode_without_offers` (explicit clock) | `writer polls saw the queue empty and the pipe under the line for 2.0s with no offer, and the episode never closed` |
| (ii) `test_writer_polls_that_see_the_pipe_backed_up_outweigh_a_few_caught_up_offers` (explicit clock) | `closed at 0.6013… s on caught-up offers while the writer's polls saw the pipe backed up for the rest of the window` |
| (iii) `test_sparse_offers_after_an_episode_close_it_and_a_burst_is_back_pressured` (live, the sticky shape) | `after 5.6 s of offers 0.7 s apart into a drained child (0 dropped) congested=True; then the burst dropped 1730 of 2000` |
| (iv) `test_quiet_then_burst_after_an_episode_drops_nothing` (live) | `after 3 s with no offers (occ=0, qsize=0) congested=True; then the burst dropped 1743 of 2000` |
| `test_sparse_offers_alone_observe_too_little_to_close_an_episode` (coverage) | `closed at 1000.6: 0.02s of observation per offer closed a window` |
| `test_a_span_opened_after_the_writers_read_survives_its_clear` (lock) | `None != 1000.5 : a span opened after the writer's caught-up read was cleared by it` |
| `test_rnstatus_names_probe_errors_as_a_running_total` | `'running total' not found in '… (congestion gate degraded)…'` |

Two pins pass on `c3c52232` by design: `test_a_put_that_waited_under_the_
floor_without_a_full_queue_does_not_sample` (`waitfloor20`: a 4 ms put with the
queue not full must not sample, a 15 ms one must) and `test_writer_polls_while_
offline_do_not_close_the_episode` (`offlineobs`). The explicit-clock tests feed
writer polls through `_writer_poll(now)`, which on a tree without
`_writer_observes` does what that tree's idle poll did (end a span, nothing
else), so they FAIL there instead of ERRORing. Every other mf.9 test is kept;
none needed its assertion changed.

**Acceptance — `tests/meshforge_pipe_tools/accept.sh`, one script, 8 legs with
their controls, sequential at load < 1.0, rc per run to files (healthy and
defective readings in its header).** moc4, same session, three trees:

| leg | `c3c52232` (mf.9, parent — red baseline) | **this tree** (mf.10) | `9120f760` (mf.8 — second control) |
|---|---|---|---|
| 1 pulse ×5 + ctl | GREEN — 0/5 entered, drops 0,0,0,1,2, 0 spans over `occ=0 qsize=0`; ctl `RATE=100000` entered 0.54 s | GREEN — 0/5, drops 1,0,1,1,0, 0 spans; ctl 0.52 s | **RED** — 0/5 entered, but 10,11,5,10,8 pre-pulse lines with a span open over a drained pipe (the F1 mechanism); ctl 0.54 s |
| 2 tick 160k + 100k | GREEN — closed ≤ 4 s into the hold, drops flat at 175; 100k ≤ 3 s | GREEN — ≤ 4 s, flat at 175; 100k ≤ 3 s | **RED** — 160k never closed in the 10 s hold (175 dropped); 100k ≤ 3 s |
| 3 sticky 0.7 s ×3 + 0.3 s + no episode | **RED** — still congested after 9.8 s of sparse traffic ×3, burst dropped 1,731 / 1,744 / 473 of 2,000; 0.3 s 0; no episode 0 | GREEN — cleared ×3, burst 0 / 0 / 0; 0.3 s 0; no episode 0 | GREEN — cleared ×3, 0 / 0 / 0; 0.3 s 0; no episode 0 |
| 4 quiet-then-burst ×3 + ctl | **RED** — flag set before the burst ×3, dropped 1,737 / 980 / 920; ctl 0 | GREEN — cleared before the burst ×3, 0 / 0 / 0; ctl 0 | **RED** — set ×3, 1,744 / 1,743 / 1,738; ctl 0 |
| 5 slowgulp 10 s | GREEN — 1 entry, 0 exits (1,645 dropped) | GREEN — 1, 0 (1,630) | GREEN — 1, 0 (1,645) |
| 6 hung (SIGSTOP 3 s) | GREEN — entered 0.52 s after the queue read full (0.91 s after SIGSTOP), 0 exits while stopped, closed 0.5 s after drained | GREEN — 0.52 / 0.91 s, 0, 0.5 s | GREEN — 0.52 / 0.91 s, 0, 0.5 s |
| 7 healthy burst | GREEN — 0 entered (1 drop: frame 321, startup) | GREEN — 0 entered (0 drops) | GREEN — 0 entered (1 drop) |
| 8 behind `rate 100000` 8 s | GREEN — 1 entry, 0 exits, congested at end (2,844) | GREEN — 1, 0, congested (2,840) | GREEN — 1, 0, congested (2,845) |
| verdict (`accept_verdict.py` rc) | 6 GREEN, 2 RED — rc=1 | **8 GREEN — rc=0** | 5 GREEN, 3 RED — rc=1 |

The brief predicted (3)+(4) red on `c3c52232` and (1)+(2) red on `9120f760`;
measured, `9120f760` is ALSO red on (4): its exit is decided only at an offer
too, so with nobody offering its flag holds through the quiet and the burst is
dropped. mf.8's sticky leg is green only because its exit had no gap reset.
Logs: moc4 `~/mf10-build/out/accept_{c3,mine,91}/`.

**Mutant matrix** (`mutants8.sh`, 40 mutants, every sed dry-run-checked to
apply — `wallclock` 10 lines, `nolock` 5, the rest 1; `FAILFAST=1`, so a
killed mutant names the FIRST failing test only; whole matrix on the final
file, frozen snapshot `~/mf10-build/tree_final`, md5 `c896454bb88b` /
`d978c763e5b7`): carried mf.9's 29 (`noidle`, `dropsonly` re-anchored on the
new lines; `quietexit` and the pass's `stickyhold` were mutations of the gap
reset that mf.10 REMOVES — on mf.9 they read as "no gap reset", which is now
the design — so `quietexit` is re-anchored to its descendant, the coverage rule
removed, and `stickyhold` to what makes the flag sticky now, the writer's poll
no longer an exit observer); added the pass's `waitedonly slowpoll endspannoop
waitfloor20 stickyhold` (`slowpoll`/`endspannoop` re-anchored) and six aimed at
the new accounting: `pollbehind` (writer polls always read backed up),
`offeronlyexit` (the writer credits but never closes), `gaprestart` (an
observation gap over `TX_PUT_WAIT_S` restarts the window — the mf.9 class on
the shared clock; a `> TX_CONGEST_FULL_S` threshold there is subsumed by the
coverage rule and was not run), `spanonlypoll` (the poll runs only while a
span is open — mf.9's cadence), `endspanstale` (the `observed_at` guard
removed), `offlineobs` (the writer observes an offline child).

`mutants.out` (moc4, 17:00–17:31, verbatim):

```
span0 rc=1 Ran 1 test in 0.094s FAILED (failures=1) 
    FAIL: test_a_burst_the_writer_absorbed_ends_the_span
never rc=1 Ran 13 tests in 16.822s FAILED (failures=1) 
    FAIL: test_child_behind_at_a_fast_rate_still_congests
always rc=1 Ran 19 tests in 55.248s FAILED (failures=1) 
    FAIL: test_congestion_episode_is_logged_at_entry_and_exit
selectpoll rc=1 Ran 14 tests in 22.307s FAILED (failures=1) 
    FAIL: test_child_behind_faster_than_a_page_per_put_wait_still_congests
nogap rc=1 Ran 7 tests in 3.727s FAILED (failures=1) 
    FAIL: test_a_quiet_gap_longer_than_the_span_restarts_it
noreset rc=1 Ran 50 tests in 130.903s FAILED (failures=1) 
    FAIL: test_the_writer_ends_the_span_after_a_write_not_only_when_idle
lowwater rc=1 Ran 3 tests in 1.616s FAILED (failures=1) 
    FAIL: test_a_late_offer_does_not_book_the_gap_before_it_as_room
countweighted rc=1 Ran 1 test in 0.539s FAILED (failures=1) 
    FAIL: test_a_burst_the_writer_absorbed_ends_the_span
noroom rc=1 Ran 40 tests in 134.520s FAILED (failures=1) 
    FAIL: test_room_that_outweighs_full_in_time_abandons_the_span
anyroom rc=1 Ran 16 tests in 40.354s FAILED (failures=1) 
    FAIL: test_child_behind_that_reads_in_gulps_congests_once_at_the_rns_mtu
nothrottled rc=1 Ran 4 tests in 2.137s FAILED (failures=1) 
    FAIL: test_a_put_that_found_the_queue_full_samples_however_short_its_wait
gapge rc=1 Ran 8 tests in 4.255s FAILED (failures=1) 
    FAIL: test_a_sample_exactly_one_span_later_still_accrues
roomge rc=1 Ran 38 tests in 111.779s FAILED (failures=1) 
    FAIL: test_room_equal_to_full_keeps_the_span
frac25 rc=1 Ran 33 tests in 115.639s FAILED (failures=1) 
    FAIL: test_pipe_full_fraction_has_a_lower_side
capfail rc=1 Ran 14 tests in 22.285s FAILED (failures=1) 
    FAIL: test_child_behind_faster_than_a_page_per_put_wait_still_congests
noclamp rc=1 Ran 9 tests in 4.800s FAILED (failures=1) 
    FAIL: test_a_sample_from_the_past_credits_nothing
wallclock rc=1 Ran 11 tests in 64.797s FAILED (failures=1) 
    FAIL: test_a_wall_clock_that_stops_cannot_hide_a_stall
rawgap rc=1 Ran 35 tests in 106.238s FAILED (failures=1) 
    FAIL: test_quiet_between_pulses_is_not_credited_as_full
noidle rc=1 Ran 28 tests in 104.586s FAILED (failures=1) 
    FAIL: test_healthy_pulse_child_between_pulses_never_congests
frac75 rc=1 Ran 14 tests in 22.278s FAILED (failures=1) 
    FAIL: test_child_behind_faster_than_a_page_per_put_wait_still_congests
everyoffer rc=1 Ran 21 tests in 56.269s FAILED (failures=1) 
    FAIL: test_exit_needs_room_for_most_of_the_window
dropsonly rc=1 Ran 3 tests in 1.597s FAILED (failures=1) 
    FAIL: test_a_late_offer_does_not_book_the_gap_before_it_as_room
nodropexit rc=1 Ran 2 tests in 1.065s FAILED (failures=1) 
    FAIL: test_a_drop_restarts_the_exit_window
nolwexit rc=1 Ran 6 tests in 3.200s FAILED (failures=1) 
    FAIL: test_a_queue_over_low_water_restarts_the_exit_window
quietexit rc=1 Ran 45 tests in 136.880s FAILED (failures=1) 
    FAIL: test_sparse_offers_alone_observe_too_little_to_close_an_episode
rawexit rc=1 Ran 3 tests in 1.598s FAILED (failures=1) 
    FAIL: test_a_late_offer_does_not_book_the_gap_before_it_as_room
nolock rc=1 Ran 18 tests in 48.520s FAILED (failures=1) 
    FAIL: test_concurrent_samples_never_credit_more_than_wall_time
capsilent rc=1 Ran 29 tests in 122.951s FAILED (failures=1) 
    FAIL: test_import_without_fcntl_and_termios
capquiet rc=1 Ran 53 tests in 177.542s FAILED (failures=1) 
    FAIL: test_unreadable_pipe_capacity_is_counted_and_logged_once
waitedonly rc=1 Ran 1 test in 0.094s FAILED (failures=1) 
    FAIL: test_a_burst_the_writer_absorbed_ends_the_span
slowpoll rc=1 Ran 19 tests in 55.799s FAILED (failures=1) 
    FAIL: test_congestion_episode_is_logged_at_entry_and_exit
endspannoop rc=1 Ran 1 test in 0.103s FAILED (failures=1) 
    FAIL: test_a_burst_the_writer_absorbed_ends_the_span
waitfloor20 rc=1 Ran 5 tests in 2.706s FAILED (failures=1) 
    FAIL: test_a_put_that_waited_under_the_floor_without_a_full_queue_does_not_sample
stickyhold rc=1 Ran 19 tests in 55.214s FAILED (failures=1) 
    FAIL: test_congestion_episode_is_logged_at_entry_and_exit
pollbehind rc=1 Ran 19 tests in 55.301s FAILED (failures=1) 
    FAIL: test_congestion_episode_is_logged_at_entry_and_exit
offeronlyexit rc=1 Ran 36 tests in 101.248s FAILED (failures=1) 
    FAIL: test_quiet_then_burst_after_an_episode_drops_nothing
gaprestart rc=1 Ran 2 tests in 1.065s FAILED (failures=1) 
    FAIL: test_a_drop_restarts_the_exit_window
spanonlypoll rc=1 Ran 19 tests in 55.267s FAILED (failures=1) 
    FAIL: test_congestion_episode_is_logged_at_entry_and_exit
endspanstale rc=1 Ran 10 tests in 5.342s FAILED (failures=1) 
    FAIL: test_a_span_opened_after_the_writers_read_survives_its_clear
offlineobs rc=1 Ran 57 tests in 147.537s FAILED (failures=1) 
    FAIL: test_writer_polls_while_offline_do_not_close_the_episode
DONE
```

**40 of 40 killed; accepted gaps: none.** Killed FIRST by a test added in mf.10: `quietexit` (coverage), `waitfloor20`, `offeronlyexit` (quiet-then-burst), `endspanstale`, `offlineobs`. `stickyhold`, `pollbehind` and `spanonlypoll` die first on the existing `test_congestion_episode_is_logged_at_entry_and_exit` — it stops offering for 2 s and then offers every 50 ms (each offer observes 20 ms of 50, under the half-window coverage), so its exit needs the writer's polls. Checked without FAILFAST against the principle tests alone (moc4 `~/mf10-build/out/newtests_vs/`): `stickyhold` and `pollbehind` fail (i), (iii) and (iv) (`Ran 3 … failures=3`); `spanonlypoll` fails (iii) and (iv) (`failures=2` — (i) calls the poll directly, so it cannot see the cadence).

**Other of record (moc4, final tree):** stall file ×3 `Ran 57 tests … OK`
rc=0 (143.3 s / 161.7 s / 143.4 s, each started at load < 1.0); the other 11
`meshforge_*` files `Ran 63 … OK` rc=0; `tests.all` `Ran 36 … OK (skipped=1)`
rc=0 (no rerun needed). Second kernel: this Pi 5 (16 KB pages) `Ran 57 … OK`
rc=0, once. rnsd untouched (moc4 `MainPID=899829 NRestarts=0` before and after).

**Not changed:** `TX_PUT_WAIT_S`, `TX_CONGEST_FULL_S`, `TX_QUEUE_DEPTH`,
`TX_LOW_WATER`, `TX_STALL_S`, `TX_PIPE_FULL_FRACTION`, the FIONREAD signal, the
entry rule and its credit, the throttled sampling and its floor, the
stall/offline logic, framing, respawn (a detached interface whose child dies is
still respawned — stock; the test fixture parks it, as in mf.9). **Not
checked:** the rnstatus line against a live rnsd; real RNS packets on 4 KB;
the matrix and the acceptance script on the 16 KB kernel (one stall-file run
there); a writer GIL-starved for whole windows while congested (the coverage
rule then HOLDS the episode — fail-safe for loss, a stale flag for rnstatus);
the poll back-off alternative was reasoned, not built. **Owed:** a FRESH
non-author pass on a 4 KB box before any tag — `accept.sh` is its first step,
then the shapes this author did not think of.

#### 2026-10-12 — Acceptance legs added after the fresh pass (arc paused)

The fresh non-author pass on `8acd4d18` read NOT TAG-READY. No gate code
changed here (`RNS/` untouched, no version bump, no tag); `accept.sh` gained
three legs that move AXES — burner count B, idle duration, frame SIZE — so the
next resume starts from a red that already exists. Drills copied from the
reviewer with a source header: `drill_gil.py` (← `gilsticky.py`),
`drill_smallgulp.py` (← `smallgulp.py`). Select with `LEGS="9 10 11"`.

- **F1 (CPU contention):** with two pure-Python spin threads the writer's idle
  poll gap stretches from 20 ms to hundreds of ms, the exit's credited
  coverage is never met, and the burst after SIGCONT meets a stale flag (leg 9).
- **F2 (idle then burst, since mf.6):** `_tx_last_drain` is the last COMPLETED
  write, so after `TX_STALL_S` + 1 s of nothing to write, the first put that
  waits reads as a 5 s stall about a healthy child (leg 10; asserts no `has
  not drained its stdin` line and `online` never False).
- **F3 (frame size, 4 KB pages):** a full queue of 30 B frames fits under the
  32 KB line, so each gulp reads as caught up and the episode flaps about a
  child still behind (leg 11).

**Load rule:** the load a B=2 run applied is the writer's idle-poll gap median
when ≥ 5 polls were seen, else `drill_gil.py`'s witness (the main thread
waiting `queue.get(timeout=TX_PUT_WAIT_S)` through the quiet). A HEALTHY B=2
run under 100 ms is UNKNOWN, never GREEN; a DEFECTIVE one stays RED; an
episode that never entered is UNKNOWN. **A leg green everywhere measured
nothing.**

Red first, moc4 (Pi 4, PAGESIZE 4096, F_GETPIPE_SZ 65536, py 3.11.2, nproc 4),
fresh clones, shim `configobj`, each run started at loadavg < 1.0; rnsd
`MainPID=899829 NRestarts=0` before and after. Verdict lines verbatim:

```
$ LEGS="9 10 11" accept.sh ~/mf11-accept/t10 out/t10   # rc=1
WHERE: tree=/home/wh6gxz/mf11-accept/t10 head=8acd4d18 gate_md5=c896454bb88b pagesize=4096 pipe_sz=65536 python=3.11.2 nproc=4 host=wh6gxzmesh legs=9,10,11 loadavg_at_start=0.41 0.74 0.66
9 sticky under CPU contention B=2 x3 + B=0 + no-episode B=2  RED      gil9_ep_b2_1:R,B=2,load=244ms(poll),wake=31,poll=244,provoked=True,cbb=True,burst_drops=1737,stall_lines=0,offline=0; gil9_ep_b2_2:UNKNOWN(episode not provoked, load=31ms(wake),wake=31,poll=-1); gil9_ep_b2_3:R,B=2,load=71ms(poll),wake=92,poll=71,provoked=True,cbb=True,burst_drops=1707,stall_lines=0,offline=0; gil9_ep_b0:G,B=0,load=20ms(poll),wake=20,poll=20,provoked=True,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil9_ctl_b2:U,B=2,load=45ms(wake),wake=45,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0
10 idle TX_STALL_S+1 then burst B=2 x5 + B=0 x3 + 3s B=2 x3  RED      gil10_idle_b2_1:G,B=2,load=229ms(wake),wake=229,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b2_2:G,B=2,load=209ms(wake),wake=209,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0; gil10_idle_b2_3:U,B=2,load=41ms(wake),wake=41,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0; gil10_idle_b2_4:R,B=2,load=960ms(wake),wake=960,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=1,offline=1; gil10_idle_b2_5:U,B=2,load=76ms(wake),wake=76,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_1:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_2:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_3:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_q3_b2_1:G,B=2,load=120ms(wake),wake=120,poll=-1,provoked=False,cbb=False,burst_drops=3,stall_lines=0,offline=0; gil10_q3_b2_2:G,B=2,load=350ms(wake),wake=350,poll=-1,provoked=False,cbb=False,burst_drops=2,stall_lines=0,offline=0; gil10_q3_b2_3:U,B=2,load=42ms(wake),wake=42,poll=-1,provoked=False,cbb=False,burst_drops=3,stall_lines=0,offline=0
11 gulp SIZE=30 x2 + rate ctl + SIZE=500                     RED      sg11_30_1:SIZE=30,CM=gulp,entries=8,exits=7,duty=0.234,congested_end=True; sg11_30_2:SIZE=30,CM=gulp,entries=10,exits=9,duty=0.229,congested_end=True; sg11_30_rate:SIZE=30,CM=rate,entries=1,exits=0,duty=0.085,congested_end=True; sg11_500:SIZE=500,CM=gulp,entries=1,exits=0,duty=0.031,congested_end=True
VERDICT: 0 GREEN, 3 RED, 0 UNKNOWN of 3

$ LEGS="9 10 11" accept.sh ~/mf11-accept/t9 out/t9   # rc=1
WHERE: tree=/home/wh6gxz/mf11-accept/t9 head=c3c52232 gate_md5=2c25c69ed3c9 pagesize=4096 pipe_sz=65536 python=3.11.2 nproc=4 host=wh6gxzmesh legs=9,10,11 loadavg_at_start=0.36 0.64 0.66
9 sticky under CPU contention B=2 x3 + B=0 + no-episode B=2  RED      gil9_ep_b2_1:UNKNOWN(episode not provoked, load=58ms(wake),wake=58,poll=-1); gil9_ep_b2_2:R,B=2,load=35ms(wake),wake=35,poll=-1,provoked=True,cbb=True,burst_drops=1743,stall_lines=0,offline=0; gil9_ep_b2_3:R,B=2,load=56ms(wake),wake=56,poll=-1,provoked=True,cbb=True,burst_drops=1740,stall_lines=0,offline=0; gil9_ep_b0:R,B=0,load=20ms(wake),wake=20,poll=-1,provoked=True,cbb=True,burst_drops=1738,stall_lines=0,offline=0; gil9_ctl_b2:U,B=2,load=32ms(wake),wake=32,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0
10 idle TX_STALL_S+1 then burst B=2 x5 + B=0 x3 + 3s B=2 x3  RED      gil10_idle_b2_1:U,B=2,load=86ms(wake),wake=86,poll=-1,provoked=False,cbb=False,burst_drops=8,stall_lines=0,offline=0; gil10_idle_b2_2:U,B=2,load=43ms(wake),wake=43,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0; gil10_idle_b2_3:U,B=2,load=44ms(wake),wake=44,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0; gil10_idle_b2_4:G,B=2,load=889ms(wake),wake=889,poll=-1,provoked=False,cbb=False,burst_drops=1,stall_lines=0,offline=0; gil10_idle_b2_5:R,B=2,load=966ms(wake),wake=966,poll=-1,provoked=False,cbb=False,burst_drops=1744,stall_lines=1,offline=1; gil10_idle_b0_1:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_2:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_3:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_q3_b2_1:G,B=2,load=279ms(wake),wake=279,poll=-1,provoked=False,cbb=False,burst_drops=5,stall_lines=0,offline=0; gil10_q3_b2_2:G,B=2,load=300ms(wake),wake=300,poll=-1,provoked=False,cbb=False,burst_drops=2,stall_lines=0,offline=0; gil10_q3_b2_3:G,B=2,load=488ms(wake),wake=488,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0
11 gulp SIZE=30 x2 + rate ctl + SIZE=500                     RED      sg11_30_1:SIZE=30,CM=gulp,entries=8,exits=7,duty=0.231,congested_end=True; sg11_30_2:SIZE=30,CM=gulp,entries=10,exits=9,duty=0.236,congested_end=True; sg11_30_rate:SIZE=30,CM=rate,entries=1,exits=0,duty=0.076,congested_end=True; sg11_500:SIZE=500,CM=gulp,entries=1,exits=0,duty=0.030,congested_end=True
VERDICT: 0 GREEN, 3 RED, 0 UNKNOWN of 3

$ LEGS="9 10 11" accept.sh ~/mf11-accept/t8 out/t8   # rc=2
WHERE: tree=/home/wh6gxz/mf11-accept/t8 head=9120f760 gate_md5=78207447b72f pagesize=4096 pipe_sz=65536 python=3.11.2 nproc=4 host=wh6gxzmesh legs=9,10,11 loadavg_at_start=0.44 0.66 0.68
9 sticky under CPU contention B=2 x3 + B=0 + no-episode B=2  UNKNOWN  gil9_ep_b2_1:U,B=2,load=31ms(wake),wake=31,poll=-1,provoked=True,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil9_ep_b2_2:U,B=2,load=74ms(wake),wake=74,poll=-1,provoked=True,cbb=False,burst_drops=11,stall_lines=0,offline=0; gil9_ep_b2_3:UNKNOWN(episode not provoked, load=31ms(wake),wake=31,poll=-1); gil9_ep_b0:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=True,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil9_ctl_b2:U,B=2,load=57ms(wake),wake=57,poll=-1,provoked=False,cbb=False,burst_drops=4,stall_lines=0,offline=0
10 idle TX_STALL_S+1 then burst B=2 x5 + B=0 x3 + 3s B=2 x3  RED      gil10_idle_b2_1:U,B=2,load=43ms(wake),wake=43,poll=-1,provoked=False,cbb=False,burst_drops=14,stall_lines=0,offline=0; gil10_idle_b2_2:R,B=2,load=350ms(wake),wake=350,poll=-1,provoked=False,cbb=False,burst_drops=1744,stall_lines=1,offline=1; gil10_idle_b2_3:R,B=2,load=277ms(wake),wake=277,poll=-1,provoked=False,cbb=False,burst_drops=1744,stall_lines=1,offline=1; gil10_idle_b2_4:G,B=2,load=325ms(wake),wake=325,poll=-1,provoked=False,cbb=False,burst_drops=2,stall_lines=0,offline=0; gil10_idle_b2_5:R,B=2,load=32ms(wake),wake=32,poll=-1,provoked=False,cbb=False,burst_drops=1744,stall_lines=1,offline=1; gil10_idle_b0_1:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_2:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_idle_b0_3:G,B=0,load=20ms(wake),wake=20,poll=-1,provoked=False,cbb=False,burst_drops=0,stall_lines=0,offline=0; gil10_q3_b2_1:U,B=2,load=31ms(wake),wake=31,poll=-1,provoked=False,cbb=False,burst_drops=7,stall_lines=0,offline=0; gil10_q3_b2_2:G,B=2,load=737ms(wake),wake=737,poll=-1,provoked=False,cbb=False,burst_drops=8,stall_lines=0,offline=0; gil10_q3_b2_3:U,B=2,load=38ms(wake),wake=38,poll=-1,provoked=False,cbb=False,burst_drops=4,stall_lines=0,offline=0
11 gulp SIZE=30 x2 + rate ctl + SIZE=500                     RED      sg11_30_1:SIZE=30,CM=gulp,entries=6,exits=5,duty=0.178,congested_end=True; sg11_30_2:SIZE=30,CM=gulp,entries=5,exits=4,duty=0.139,congested_end=True; sg11_30_rate:SIZE=30,CM=rate,entries=1,exits=0,duty=0.061,congested_end=True; sg11_500:SIZE=500,CM=gulp,entries=1,exits=0,duty=0.030,congested_end=True
VERDICT: 0 GREEN, 2 RED, 1 UNKNOWN of 3
```

Expected vs read: leg 10 RED ×3 and leg 11 RED ×3 as claimed; leg 9 RED on
`8acd4d18` as claimed. **Did not read as claimed:**
(a) leg 9 on `9120f760` reads **UNKNOWN, not GREEN** — its healthy B=2 runs
show a witness of 31–74 ms, so the rule refuses the green. The witness
under-reads the writer: on `8acd4d18` run 1 the writer's poll gap was 244 ms
while the main thread's was 31 ms (across calibration and these runs: 0.1–1.25×). Trees without
`_writer_observes` have no writer-side witness; a gate seam (a writer-wake
timestamp) would be needed to measure contention where it bites — not added.
(b) leg 9 on `c3c52232`: its B=0 control is RED too (1,738 drops, flag held)
— the mf.9 sticky exit (0.7 s offer gap > 0.5 s window) fires without any
contention, so leg 9 cannot separate F1 from mf.9's own defect on that tree.
(c) with burners started BEFORE the provoke (the acceptance shape),
`8acd4d18` often does not enter within the reviewer's 1.5 s SIGSTOP budget
(3 of 4 calibration runs; 9120f760 4 of 4 entered) — the entry credit is
starved like the exit's. Leg 9 uses `PROVOKE_S=4`; 1 of 3 runs per tree still
did not enter (UNKNOWN). Entry latency under contention is unmeasured as a leg.
(d) F2 is intermittent: stall lines in 1/5 (`8acd4d18`), 1/5 (`c3c52232`),
3/5 (`9120f760`) B=2 runs, 0/9 at B=0, 0/9 at 3 s idle B=2.
Also fixed in `accept.sh`: a relative `<outdir>` broke every run (the runs
`cd` into the tree); OUT is now made absolute. Logs:
`~/.local/share/meshforge/mf7-review-tools/mf11-accept/` on the MeshForge
manager box.

### `1.3.8+mf.9` (UNRELEASED) — the congestion gate credits only time it OBSERVED, and closes an episode on what the domain loses

Built red-first ON a 4 KB-page box (moc4, Raspberry Pi 4, Python 3.11.2,
`F_GETPIPE_SZ` 65536 → the 50 % line is 32 KB) by a session that did NOT
review mf.8; the spec is the non-author pass on `9120f760` (the mf.8
candidate), which found it NOT tag-ready with two defects reproduced live on
this box and five surviving mutants. This cut is still an AUTHOR's cut: it
carries no non-author pass and is not tagged; `RNS/_version.py` is not
bumped (it still reads mf.7 — the bump belongs to the release commit).

What the pass measured (moc4, its logs under `~/mf8-review/out/drills/`):

- **F1 — unobserved time credited as full (the blocker; a regression of the
  mf.6 short-burst class).** `_note_pipe_full(now)` credited the WHOLE gap
  since the previous sample to the state the pipe was in at the next one.
  Pulse shape (`drill_pulse_x.py`): 4 × 100 × 1000-B frames every 0.25 s
  into a `rate 2000000` child — healthy, 1.6 MB/s mean into 2 MB/s. Between
  pulses the pipe read `occ=0 qsize=0` with `_tx_full_since` still set (the
  writer's caught-up reset ran only AFTER a write, and a burst's last write
  leaves the pipe over the line); the next pulse's first waited put booked
  0.263 s of that quiet as full. 1 of 5 runs congested at 0.54 s with 0
  drops before entry and 1,204 after (0–1 in the runs that did not), and
  never exited.
- **F4 — an episode never closed for a child that keeps up on a tick.** Exit
  needed EVERY offer across `TX_CONGEST_FULL_S` to see the queue empty and the
  pipe under the line. A child that reads everything every 0.25 s
  (`drill_tick.py`), provoked at 400 kB/s then held at 160 kB/s, keeps up —
  drops flat at 175 — while each tick peaks ~39 KB, so ~28 of 160 offers/s
  reset the run: congested for the whole 10 s hold, no exit line. Control at
  100 kB/s (peaks ~25 KB) exited in 1.8 s.
- **F2** `import fcntl` / `import termios` unguarded; `Reticulum` star-imports
  every interface, so `import RNS` failed without them (Windows).
- **F3** an `F_GETPIPE_SZ` failure cached capacity 0 and fell back SILENTLY to
  the `select()` free-page poll — the signal mf.8 proved wrong on 4 KB.
- **F5** no lock between sender threads in `_note_pipe_full`: two senders
  released together read the same previous-sample clock and both credit it.
- **Surviving mutants on 4 KB:** `gapge`, `roomge`, `frac25`, `noclamp`,
  `wallclock` (`capfail` was killed).

What changed (`RNS/Interfaces/PipeInterface.py`; measure, don't assert —
time the gate never saw is not evidence for either side):

1. **Entry credits only observed waits (F1a).** Each sample is credited with
   `min(gap since the previous sample, the time THIS put waited)` — to full
   or room time as before. A put waits at most `TX_PUT_WAIT_S`, so a sample
   can never book more than that; a sender that is back-pressured spends its
   time waiting, so a child that is really behind still accrues full time at
   wall-clock rate (the caller has fallen behind its schedule and stops
   sleeping between puts). The opening sample of a span still credits 0.
2. **The writer observes the quiet (F1b).** While a pipe-full span is open
   the writer's idle wait is `TX_PUT_WAIT_S` instead of 0.5 s, and when it
   times out with the queue empty it checks `_child_caught_up()` (queue empty
   AND pipe under the line) and ends the span — not only after a write. The
   quiet-gap abandon rule (`> TX_CONGEST_FULL_S` with no waited put) is kept.
3. **Exit is a time-weighted window, ended outright by a loss (F4).** While
   congested, every offer: a frame dropped since the previous offer, or the
   queue over `TX_LOW_WATER`, ends the window (none open); a gap of more than
   `TX_CONGEST_FULL_S` between offers restarts it at this offer (unobserved
   time, not caught-up time); otherwise the offer is credited
   `min(gap, TX_PUT_WAIT_S)` — an offer observes one instant — to "caught
   up" (queue empty, pipe under the line) or not. When the window is
   `TX_CONGEST_FULL_S` old the episode closes if caught-up time exceeds the
   rest, else the next window starts. Same constant, same weighing as entry
   (hfm #5). A gulp child still holding a backlog (16 KB pages, the queue dump
   fits the emptied pipe) is over the line for most of every window and stays
   congested; a tick child that keeps up is under it ~80 % of the time and
   closes.
4. **F2** fcntl/termios imported under `try/except ImportError` (`None`);
   without them `_pipe_full` takes the counted `select()` fallback.
5. **F3** a capacity that cannot be read increments `tx_probe_errors` (once
   per child) and logs ONE WARNING per interface naming the `select()`
   fallback and why it is weaker; `Reticulum.get_interface_stats` exposes
   `tx_probe_errors` beside `tx_dropped`/`tx_congested`, and `rnstatus` prints
   `Pipe probe: N failed read(s) of the stdin pipe state (congestion gate
   degraded)` under `Dropped` when it is non-zero.
6. **F5 — a lock (`_tx_gate_lock`), justified by a test.** Per-put crediting
   bounds a collision's double credit by `TX_PUT_WAIT_S`, but does not remove
   it, and that double credit inflates exactly the quantity that decides
   drops. Under the lock the credits of one span sum to at most its wall
   time — an invariant `test_concurrent_samples_never_credit_more_than_wall_
   time` checks with the read of the span's clock made to yield the GIL. The
   ioctl stays OUTSIDE the lock; the entry/exit transitions and the writer's
   span clear take it. A sample that arrives with a clock older than the last
   (taken before the lock) credits nothing and never moves the span's clock
   backward.
7. Class comments that still described the mf.7 "abandon when a put finds
   the pipe writable" rule are corrected; the entry line now names the
   quantity measured: `callers waited N s for a queue slot with its stdin
   pipe at least 50% full`, and states the exit rule.

**Exit-on-quiet decision:** with no offers the congested flag HOLDS (no
writer-side exit). Nothing is lost while nothing is offered, and quiet is not
evidence the child caught up; the first `TX_CONGEST_FULL_S` of offers after
the quiet decide (they are offered without waiting, so a caught-up child
costs nobody anything in that window). `rnstatus` may read "child behind,
dropping now" through an idle period after an episode — a stale flag, not a
loss. Stock behaviour has no flag at all.

> **Contradicted by measurement (mf.10):** the flag held through sustained sparse traffic, not only idle, and the next burst into a healthy child was dropped (1,557–1,741 of 2,000, non-author pass on `c3c52232`) — see `1.3.8+mf.10` above.

Tests (`tests/meshforge_pipe_interface_stall.py`, 28 → 49): 21 new. **Red-first — the
final file run against the `9120f760` gate on moc4 (`tree9red` = this tree
with `PipeInterface.py`/`Reticulum.py`/`rnstatus.py` from `9120f760`, gate md5
identical to the review's tree): `Ran 48`, FAILED (failures=11), rc=1:**

| test | on `9120f760` (moc4) |
|---|---|
| `test_quiet_between_pulses_is_not_credited_as_full` (F1a, explicit clock, the review's sequence) | `a healthy pulse child congested: 1.765s booked as full against 0.260s of observed waits` |
| `test_writer_idle_with_a_drained_pipe_ends_the_span` (F1b) | `… is not None : an idle writer left a span open over a pipe with room and an empty queue` |
| `test_healthy_pulse_child_between_pulses_never_congests` (F1 live, 8 pulses) | `Lists differ: [1, 3, 4, 5, 6, 7] != []` — pulses that began with a span open over a drained pipe |
| `test_tick_child_that_keeps_up_exits_congestion` (F4 live) | `a tick child keeping up at 164 kB/s stayed congested for 8 s (156 dropped)` |
| `test_exit_needs_room_for_most_of_the_window` (F4, explicit clock) | `pipe under half for 80 % of 3 S of offers, no drops: never caught up` |
| `test_a_drop_restarts_the_exit_window` | `caught up at 1.00 S, less than a window after the drop at 0.4 S` |
| `test_a_quiet_gap_between_offers_restarts_the_exit_window` | `caught up at 1.20 S: the quiet gap was counted as caught up` |
| `test_concurrent_samples_never_credit_more_than_wall_time` (F5) | `0.210s booked over a span 0.053s long` |
| `test_import_without_fcntl_and_termios` (F2, subprocess with the modules blocked) | `ModuleNotFoundError: import of fcntl halted` |
| `test_unreadable_pipe_capacity_is_counted_and_logged_once` (F3) | `0 != 1 : an unreadable pipe capacity was not counted (once)` |
| `test_interface_stats_carry_probe_errors` (F3) | `None != 3` |

Ten more are PINS (they pass on `9120f760` by design; each exists to kill a
named mutant): `test_the_writer_ends_the_span_after_a_write_not_only_when_idle`
(`noreset` — added after round 1 of the matrix, below; the idle poll is
patched out of reach so only the post-write check can end the span; it was
added after the red run, so it is in the final file but not in that `Ran 48`), `test_tick_child_held_behind_stays_congested` (the live control
for the tick exit: 1.5x what the child can take for 7 s → one episode, still
congested), `test_exit_holds_while_the_pipe_stays_backed_up_without_drops`
(`dropsonly`), `test_a_queue_over_low_water_restarts_the_exit_window`
(`nolwexit`), `test_a_late_offer_does_not_book_the_gap_before_it_as_room`
(`rawexit`), `test_a_sample_exactly_one_span_later_still_accrues` (`gapge`:
exact binary clocks, gap == S accrues), `test_room_equal_to_full_keeps_the_
span` (`roomge`), `test_pipe_full_fraction_has_a_lower_side` (`frac25`/`frac75`:
a real pipe 35 % full is NOT backed up, 65 % is), `test_a_sample_from_the_past_
credits_nothing` (`noclamp`), `test_a_wall_clock_that_stops_cannot_hide_a_stall`
(`wallclock`: `time.time` frozen, a SIGSTOPped child still goes offline after
`TX_STALL_S` — a test, not a clock seam). Existing explicit-clock tests now
feed samples through `_note(now, waited)` (default: the put waited the whole
interval, so their arithmetic is unchanged; on a tree whose gate takes no
`waited` the sample is fed without it, so a new test FAILS there instead of
ERRORing) and park the writer (`_park_writer`) so its new idle check cannot
race a hand-fed sequence.

**A fixture defect found on the way (test-only fix):** the stock respawn loop
(`reconnect_pipe`) does not look at `detached`, so every test's killed child
was respawned every `respawn_delay` for the rest of the run into a tmp dir
that no longer existed — 251–320 `can't open file … child.py` lines per run
in the review's own `9120f760` logs, ~840–1,030 with the new tests, load 4–10
on the Pi 4 by the end of the file. The first full run of this cut on moc4
FAILED `test_tick_child_that_keeps_up_exits_congestion` (`35 != 18` — drops
in the second after exit) at load 10.7; that run is archived, not counted.
tearDown and `_swap_iface` now park the loop (`respawn_delay = 3600`); every
run below logs 0 such lines and finished at load < 1. Production is NOT
changed: a detached PipeInterface whose child dies is still respawned (stock
behaviour; not this cut's question, recorded here for the next one).

**Of record (moc4, Pi 4, 4 KB pages, py3.11.2, final tree — `PipeInterface.py`
md5 `2c25c69ed3c9…`, stall file `3d34b1aaf471…`; every run started at load
< 1.0, rc captured to a file, logs under `~/mf8-review/out9/`):**

- stall file ×3 `Ran 49 tests … OK`, rc=0 (144.5 s / 140.5 s / 150.9 s),
  0 respawn-noise lines; `meshforge_*` (12 files) `Ran 112 … OK` rc=0;
  `tests.all` `Ran 36 … OK (skipped=1)` rc=0 (on the 48-test file; it does
  not include the stall file). The earlier 48-test file also passed ×3 on
  moc4 (`out9/stall{1,2,3}`). Second kernel, not required: the manager Pi 5
  (16 KB pages, py3.13) `Ran 49 … OK` rc=0, once.
- **Mutant matrix** (`tests/meshforge_pipe_tools/mutants8.sh`, 29 mutants,
  every sed applied — dry-run checked before the run): round 1 on the
  48-test file, **28 of 29 killed; `noreset` SURVIVED** (the new idle check
  also ends the span after an absorbed burst, so the existing pin no longer
  isolated the post-write check). Pin added; round 2 on the final 49-test
  file: `noreset rc=1 … FAILED (failures=1)` — killed by
  `test_the_writer_ends_the_span_after_a_write_not_only_when_idle`. **29 of
  29 killed; accepted gaps: none** (`wallclock` is killed by the frozen-clock
  stall test, so no clock seam was added). Only the survivor was re-run on
  the final file — adding a test cannot revive a killed mutant, the other 28
  tests were unchanged — but the whole matrix was NOT re-run on it. Killed by
  ONE test each (the next pass should know where the matrix is thin):
  `nogap`, `noroom`, `nothrottled`, `gapge`, `roomge`, `wallclock`, `rawgap`,
  `nodropexit`, `nolwexit`, `quietexit`, `rawexit`, `nolock`, `capquiet`,
  `noreset`.
- **The review's drills, re-run against this tree (the external witness —
  tests this session did not write; `drill_pulse_x.py`'s spy adapted to the
  gate's new `(now, waited)` signature, nothing else):** pulse (`LOG=1`,
  20 pulses, 400 × 1000 B every 0.25 s into 2 MB/s) **0 of 10 entered**,
  drops per run 0,0,0,2,0,0,0,0,2,2; then an INTERLEAVED A/B, `9120f760`
  (A) vs this tree (B), 10 pairs: **A entered 1 of 10** (A10 at 4.01 s,
  339 dropped, still congested at the end — F1 reproduced), **B 0 of 10**;
  drops in the runs that did not enter A 1,2,0,2,0,2,0,0,0 / B
  0,0,1,1,0,0,2,0,2,0. So: B 0 of 20 entered. The brief's "≤ 1 drop per
  run" is NOT met — 5 of 20 B runs dropped 2 — but the un-entered baseline
  drops the same (3 of 9 A runs at 2): those are back-pressure timeouts
  outside congested mode (a put that waited `TX_PUT_WAIT_S` for a slot; the
  gate decides nothing there), not the gate. Per-pulse state lines show no
  span open over `occ=0 qsize=0` (0 of 200 pre-pulse lines, 10 runs). Tick
  (`drill_tick.py`, provoke 400 kB/s 3 s, hold 10 s): **160 kB/s — entered
  at 0.51 s of observed waits, closed ~3 s into the hold** (`caught up; 150
  frame(s) dropped during the episode, 175 total`) once the provoked backlog
  had drained (offers ≥ 50 %: 158, 160, 112, then 28 per second), drops flat
  at 175 for the remaining 6 s; control 100 kB/s closed ~1.8 s into the hold
  (as on `9120f760`), drops flat at 175.

**Not changed:** `TX_PUT_WAIT_S`, `TX_CONGEST_FULL_S`, `TX_QUEUE_DEPTH`,
`TX_LOW_WATER`, `TX_STALL_S`, `TX_PIPE_FULL_FRACTION` (0.5), the FIONREAD
byte signal, the throttled sampling, the stall/offline logic, framing,
respawn. **Not checked:** the rnstatus `Pipe probe` line was not rendered
against a live rnsd (no RNS instance was started; `get_interface_stats` is
tested); Windows itself (the import test blocks the modules on Linux); a
child that hovers exactly at rate with a standing backlog over half the pipe
stays congested at zero drops (flag only — offers do not wait); real packets
on 4 KB; the 16 KB kernel beyond one stall-file run (no matrix, no drills there). **Owed:** a FRESH non-author pass on a
4 KB box before any tag, with the pulse drill ×≥10.

### `1.3.8+mf.8` (UNRELEASED) — the PipeInterface congestion gate reads the pipe in bytes, not pages, and weighs its evidence by time, not by sample count

Built red-first ON a 4 KB-page box (moc4, Raspberry Pi 4, Python 3.11.2),
where the `1.3.8+mf.7` tag's own stall file failed 5/23, 4/23, 4/23 from a
clone before any install (operator: "go — mf.8 red-first on moc4"). Seven of
the fleet's nine RNS boxes are Pi 3/4 with 4 KB pages and a 64 KB pipe; the
whole mf.6/mf.7 arc — build and six non-author passes — ran on the two Pi 5s
(16 KB pages, 256 KB pipe) and never saw the kernel class the gate ships to.
Measured first (`drill_occ.py`, per put that waited: `select()` writability
and `FIONREAD` occupancy, both kernels):

| scenario (moc4, 4 KB) | select() writable | occupancy ≥ 50 % | mf.7 outcome |
|---|---|---|---|
| child behind at 800 kB/s, 1.2 MB/s offered | 75 % of samples | 100 % (median 0.84, min 0.69) | never congested; caller busy 3.5 s of 4 s |
| gulp child 200 kB/s, 1000 B at 600 f/s | 66 % | 57 % by count — alternating 5/5 full and 24/52 room per 100 ms | never congested in 4 s |
| healthy child, 2000-frame burst | 99 % | 9 % (median 0.00) | 1 frame dropped: frame 321 = 64 + 256 + 1, every run |

Two page-size couplings in the gate, one in a test:

1. **"Pipe full" was a free-page poll.** `select()` reads writable the moment
   ONE page is free, and on 4 KB pages a child behind at 800 kB/s frees a
   page every ~5 ms, so three samples in four read writable on a pipe that
   was 69–100 % full, no span ever formed, and the forwarding caller paid
   the mf.6 saturation (4.3–4.6 s of every 5 s). The kernel fact that does
   not depend on the page size is how many BYTES are buffered: `FIONREAD`
   on the write end (works on both kernels) against `F_GETPIPE_SZ`. The pipe
   is backed up when it holds at least `TX_PIPE_FULL_FRACTION` (0.5) of its
   capacity. Where the capacity cannot be read (not Linux) the free-page
   poll is kept; an ioctl failure is counted (`tx_probe_errors`) and reads
   NOT full, as before. A put that found the queue FULL on arrival now
   samples regardless of how long it waited (on 4 KB pages that wait is
   under `TX_PUT_WAIT_S / 2`, so the wait-length rule alone never sampled).
2. **The span was abandoned on ANY room sample, and samples cluster in
   time.** A gulp child empties a 64 KB pipe every ~0.3 s and the writer
   refills it in a few ms; every put that waited through the full phase is
   released in those ms, so 20–50 room samples land in one 10 ms window
   against 5 full ones (one per 20 ms timeout), and the span never reached
   `TX_CONGEST_FULL_S`. Each sample now stands for the time since the
   previous one, credited to what the kernel says at that sample: the span
   congests when full time reaches `TX_CONGEST_FULL_S`, and is abandoned
   when room time exceeds full time (a healthy child under GIL hogs leaves
   the pipe with room for most of the time the caller waits — the writer,
   not the child, is behind), on a quiet gap of `TX_CONGEST_FULL_S` with no
   waited put, or by the writer on a caught-up child, as before. Exit is
   unchanged in form: queue empty AND pipe not backed up, persisting across
   `TX_CONGEST_FULL_S` of offers.
3. **The healthy-burst test measured child startup.** A just-spawned Python
   child reaches its first read 30–100 ms after `Popen` on a Pi 4; a burst
   offered before that fills pipe + queue and the first frame past them
   (321 on a 64 KB pipe) waits `TX_PUT_WAIT_S` for a child that is not
   reading yet and is dropped — deterministically, on mf.6 too. With the
   child confirmed reading first: 0 of 2000 dropped, 3/3. The tests about a
   HEALTHY child now start from a child that reads (`_wait_child_reading`).
   The production shape is real and small: frames offered past the buffers
   in a child's first ~100 ms are dropped after 20 ms, and counted.

Tests: `test_pipe_full_reads_occupancy_not_a_free_page` (child stopped, pipe
filled to 75 % by hand: `select()` writable, `_pipe_full()` must be True —
red on mf.7 on both kernels) and
`test_room_samples_clustered_in_time_do_not_abandon_the_span` (explicit
clocks: 0.3 S of full samples, 30 room samples inside 0.01 S one timeout
later, full again — survives, congests at 1.1 S; red on mf.7 on both
kernels) are the red-first pair; `test_room_that_outweighs_full_in_time_
abandons_the_span` pins the other side (0.5 S full, 0.6 S room abandons,
0.5 S full in the fresh span must NOT congest — a gate that never abandons
on room has 1.0 S of full time there and does; the fresh span congests at
1.1 S, so the shape is not vacuous). Stall file on this cut (before the two extra pins): moc4
(4 KB, py3.11) `Ran 26 OK` rc=0, VolcanoAI (16 KB, py3.13) `Ran 26 OK` rc=0;
`meshforge_*` 63 OK, `tests.all` 36 OK (1 skipped). Two more deterministic
pins came out of the matrix: `test_caught_up_needs_an_empty_queue_and_a_
pipe_with_room` (the `lowwater` leg survived the whole file on moc4 — on a
64 KB pipe the queue dump does not fit, so the gulp shape pins nothing
there) and `test_a_put_that_found_the_queue_full_samples_however_short_its_
wait` (the `throttled` leg survived on both kernels: at 800 kB/s jitter
waits sample anyway). Final stall file `Ran 28 OK` rc=0 on both kernels.
Mutant matrix (`tests/meshforge_pipe_tools/mutants8.sh`, pattern-anchored:
span0, never, always, selectpoll = the mf.7 free-page poll, nogap, noreset,
lowwater, countweighted, noroom, anyroom = the mf.7 abandon rule,
nothrottled) run on BOTH kernels: **all 11 killed on both** (selectpoll by
1 test on 16 KB — the deterministic occupancy test — and 3 on 4 KB; anyroom
by 1 on 16 KB and 3 on 4 KB; the rest identical). Tools kept in
`tests/meshforge_pipe_tools/` (drills + matrix + README). Not changed: `TX_PUT_WAIT_S`, `TX_CONGEST_FULL_S`,
`TX_QUEUE_DEPTH`, `TX_STALL_S`, the writer thread, framing, respawn.
Non-author pass owed before any tag; this session wrote the code and never
tags. **For every future PipeInterface pass: `getconf PAGESIZE` across the
fleet first, and the stall file on a 4 KB box BEFORE the verdict.**

### `1.3.8+mf.7` (2026-10-12) — a PipeInterface child that is behind costs a forwarding reader one wait per congestion episode, and stops flapping

Transport-forwarding drill 2026-10-10 (three private instances: TCP client →
router with `enable_transport = Yes` → PipeInterface → relay child → TCP →
far instance; 100 forwarded frames/s + 25/s control to the router itself):
with `enable_transport = Yes` a forwarded frame is transmitted on the reader
thread of the interface it ARRIVED on, so on `1.3.8+mf.6` a child that drains
but slower than it is offered made every frame past its drain rate wait
`TX_PUT_WAIT_S` (20 ms) on that reader. The child kept completing writes, so
the stall check never fired: at 4 kB/s the control traffic on the arriving
TCP interface — which never touches the pipe — ran 8 s median / 20.5 s
worst, the pipe never Down. At 2 kB/s the interface instead flapped Down/Up
every ~8 s: on a 16 KB-page kernel the writer gets room only when the child
has consumed a whole page (16 KB ÷ 2 kB/s > `TX_STALL_S`), and the first
completed write flipped it back with the queue still full. A SIGSTOPped
child was fine (1.23 s worst, ×2); stock was worse in every shape (rnsd's
own announcing thread froze). Cure: when puts have waited for a slot (or
timed out) while the KERNEL reports the pipe full (`select()` says not writable — the writer is
parked in `flush()`) for `TX_CONGEST_FULL_S` (0.5 s), the interface is
CONGESTED — frames are offered with `put_nowait` (dropped and counted only
if the queue is full) until the child has CAUGHT UP: the queue is empty AND
the kernel pipe has room (`_child_caught_up`), and has STAYED so across
`TX_CONGEST_FULL_S` of offers with no put finding the queue full
(`_caught_up_for` — exit needs persistent evidence, as entry does; one
constant for both). A caller pays at most `TX_CONGEST_FULL_S` of
waits per episode (one caller: ~25 × 20 ms; measured 27 in a 5 s run at
200 kB/s drained / 600 f/s offered, where mf.6 paid 187, i.e. 75 % of the
caller's time, for the run's whole length), then none. Congested mode has
NO back-pressure, which is why the evidence has to be the kernel's AND has
to persist. Three cuts used weaker evidence and each fresh pass broke it:
`94cd084d` congested on ANY timeout, so a HEALTHY child whose writer had
merely not been scheduled (4 senders + 1 CPU-burning thread) dropped
1242–1739 of 2000 where `1.3.8+mf.6` dropped 67–117; `a67be362` congested
when the writer had sat in one write call for 0.1 s, which (a) also counts
the GIL re-acquire after the syscall, so long GIL holds on other threads
still congested a healthy child (1739 of 2000 dropped vs 207 on mf.6), with
the pipe writable when sampled, and (b) missed a child behind at a FAST
rate: the kernel wakes a pipe writer only per consumed PAGE, so a 200 kB/s
child on 16 KB pages blocks ~80 ms per write and never qualified — every
frame paid `TX_PUT_WAIT_S` again (496 waits / 15 s, caller busy 67 %; on a
4 KB-page kernel that band would start near 41 kB/s); `6adfa33b` congested
on ONE pipe-full sample, and a pipe is full the moment the child is
unscheduled: a `nice 10` child beside 12 busy processes, offered a 2 MB
burst it drained in 0.3 s (slower than the senders for the burst's length,
so behind by the sample's definition, yet it delivered all of it on mf.6
for 19–44 waits) dropped 1487 of 2000 — exactly the 256 KB pipe + 256-frame
queue, then everything — in three of three interleaved runs; a 60 ms span
of samples dropped 1220–1487 the same way. What separates a burst from the
overload this cure is for is DURATION, so the gate is a span of pipe-full
timeouts, 0.5 s: far above scheduler latency on a loaded Pi, far below
`TX_STALL_S` (5 s) and the 8 s saturation measured on mf.6. The span is
abandoned when a put that waited or timed out finds the pipe writable (the writer is not
blocked — a starved writer is not a child behind), when the writer finds the
child caught up (queue empty AND pipe writable — the burst was absorbed), or
when no put has waited or timed out for `TX_CONGEST_FULL_S`. "Caught up" was first cut
as "the queue drained to `TX_LOW_WATER`" (128 frames) and the fresh pass on
`925d275c` broke it (F1): a child that is behind at a steady rate but reads
in GULPS — one `os.read` of a large buffer empties the whole 256 KB pipe —
makes that true every cycle: the writer dumps the whole queue into the
emptied pipe, the queue reads empty, the episode closes and the span resets
while the child is as far behind as before, and the reader pays the next
0.5 s of waits again: at 200 kB/s drained / 300 f/s offered, 9 episodes in
30 s, 451 waits, reader duty 0.31 (6adfa33b: 0.02; mf.6: 0.78), "caught up"
logged nine times about a child that never did. It was also page-size
coupled (128 KB of low water is below a 256 KB pipe on 16 KB pages; on a
4 KB-page kernel the 64 KB pipe cannot free 128 slots in one gulp, so that
leg could not fire that way there). With caught-up = queue EMPTY and pipe
WRITABLE the same shape is 1 episode that never closes, 44 waits, duty 0.03,
bytes delivered unchanged (6 372 720 B) — but only at 1000-B frames, and the
fresh pass on `b18aae01` broke it (F1): a pipe write merges into the last
page only if it fits, so 1002-B frames pack 16 to a 16 KB page and the
256-frame queue dump fills all 16 pages (not writable — right by
coincidence), while 502-B frames (the RNS MTU) pack 32 to a page, the dump
fills 8 of 16, `select()` reads writable with the child 1.3 s behind, and
the gate re-armed every ~1.3 s: gulp child at 500 B / 1200 f/s for 20 s,
14–15 episodes, 368–393 waits, reader duty 0.38–0.41 (6adfa33b: 16 waits,
0.027). One caught-up SAMPLE is not evidence; a run of them is: a child
behind refills the queue from the offered stream within ~0.2 s of the dump
and cannot keep it empty for 0.5 s. With the exit duration the same shape is
1 episode, 35–36 waits, duty 0.05, bytes delivered unchanged (4 105 858 B
vs 4 079 252–4 106 862 B on `b18aae01`), and the 1000-B shapes are
unchanged (1 episode, 26 waits). The earlier "sound on 4 KB pages, broken
on 16 KB" reading is inverted: the single-sample rule held on 4 KB pages
(a 64 KB pipe is always over-filled by the dump) and broke on 16 KB pages
at frames under ~910 B. Every test and drill in this file now offers 500-B
frames first, 1000 B second. With this gate the `nice 10` shape reads
as mf.6 (11–23 dropped vs 13–54, 0 episodes, three interleaved pairs); the
GIL-contention shapes stay at 0 episodes with drops within mf.6's own
run-to-run spread (8 senders + 2 burners: mf.6 1126–1793 vs 1126–1330); and
a child behind at 200 kB/s congests because it times out puts every
`TX_PUT_WAIT_S` with the writer parked in `flush()` (100 % of 1501 samples
per 5 s run). A child behind at a FASTER rate did not (fresh pass on
`b18aae01`, F2): when it frees a page within `TX_PUT_WAIT_S` (16 KB / 20 ms
= 800 kB/s on 16 KB pages; ~200 kB/s on 4 KB pages) every put waits up to
20 ms and then SUCCEEDS, no put times out, the pipe was never sampled, and
the caller paid a wait per frame as on mf.6 (800 kB/s child offered
1.2 MB/s: 0 episodes, 186–187 waits, duty 0.76). The pipe is now sampled on
every put that WAITED ≥ `TX_PUT_WAIT_S / 2` as well: 1 episode, 25–43
waits, duty 0.12–0.19, delivered 4 041 050–4 051 070 B vs 4 007 046–
4 041 050 B (the ~1530 extra drops are the frames mf.6 held back upstream,
as below). A healthy child whose writer is merely slow leaves the pipe
writable at those samples. The trade, stated: once a child is behind for 0.5 s, the excess
is dropped at the pipe instead of stalling the reader that offered it — at
200 kB/s drained / 300 kB/s offered, 1016 frames dropped in 15 s vs 374
when the reader was held back (and every other frame on that reader with
it) — and the fact that settles it: bytes delivered to the child were
IDENTICAL on mf.6, `a67be362`, `6adfa33b` and this cut (3 338 664 B at
200 kB/s, 4 878 738 B at 300 kB/s; 1 347 690 B in the 5 s/600 f/s drill);
the extra drops are exactly the frames mf.6 held back upstream. Every
episode leaves a witness (fresh pass on `6adfa33b`, F3 — `tx_dropped` had
no reader at all, so the band's designed drops were invisible while
`rnstatus` read Up): one WARNING when the child is behind, one when it has
caught up — or when a respawn ends the episode ("respawned mid-episode",
fresh pass on `b18aae01`, F3: 2 of 3 respawned episodes had no closing
line) — with the episode's drop count; every offer refused while the
interface is offline is counted in `tx_dropped`, not only those refused
while stalled (same F3: 1363 frames vanished uncounted in a respawn drill); `rnstatus` prints a `Dropped` line
(count + whether the child is behind now) for a PipeInterface that has ever
dropped a frame, from `get_interface_stats` keys `tx_dropped` /
`tx_congested`. `TX_LOW_WATER` (`TX_QUEUE_DEPTH // 2` = 128) gates recovery from a stall: the
interface comes back Up when a write completes AND the queue is at or below
`TX_LOW_WATER`, so a slow child no longer flaps on every page — at the
price of a longer Down: time-to-Up after a stall is about `TX_LOW_WATER`
frames ÷ the child's drain rate (~128 KB of 1 KB frames: measured 72 s at
2 kB/s, longer at LoRa rates), while bytes delivered are unchanged (fresh
pass F2). If `select()` itself fails the pipe reads as NOT full — without
evidence the interface keeps mf.6 back-pressure — and the failure is
counted (`tx_probe_errors`, first one logged at DEBUG), so a permanently
broken probe is not a permanently invisible one. The span start is read
ONCE per timed-out put: the writer clears it without a lock, and a clear
landing between a second read and the subtraction raised `TypeError` on the
TX path (fresh pass on `925d275c`, F2 — replayed deterministically, 0 live
hits in 9 runs; Transport would have logged it as an ERROR with the drop
uncounted). Not changed: stall
detection (still the drop path, still `TX_STALL_S` from the last completed
write), queue depth, respawn (beyond the episode line and the offline
count above). Known and bounded: during `configure_pipe`'s
1 s join after a child death the retired writer may still take live-queue
frames into the dead child's pipe (≤ one pipe's worth). Tests (in
`tests/meshforge_pipe_interface_stall.py`; congestion entry is recorded by a
property spy, because a polling watcher thread is starved by the very
contention under test; each leg of the gate has a test that fails when that
leg is removed, measured on this cut — span → 0: the paused-child,
short-burst, quiet-gap and mid-sample tests; `select` always "not full": the
fast-rate, gulp-child, episode-log and slow-child tests; `select` always
"full": the starved-writer, both GIL tests, the paused-child and episode-log
tests; the quiet-gap abandon rule deleted: the quiet-gap and mid-sample tests
(fed explicit clocks; the fresh pass on `925d275c` found this leg unpinned,
F3); the writer-side caught-up reset deleted: the paused-child test;
caught-up reverted to "queue at low water": the gulp-child test only —
superseded on the `b18aae01` follow-up cut, see "Open" below; the exit
duration → 0: the 500-B gulp test; waited-put sampling removed: the
800 kB/s test at both sizes; the offline counter reverted: the crash test;
the respawn episode line removed: the crash test): a
child started with `slow` (200 B per 100 ms) — on
`5f9d0553` (= mf.6 code) the shipped test FAILS on the waits (the budget is
computed with a `getattr` default so a tree without the gate fails on the
property, not on the name — F4 of the same pass; the "100 of 100 waited,
2.01 s" figure is what the `select`-always-"not-full" mutant produces), now
≤ `TX_CONGEST_FULL_S / TX_PUT_WAIT_S` + 1; SIGSTOP → offline → SIGCONT — Up
with 256 frames still queued on mf.6, now ≤ 128; a HEALTHY child under 4
senders + 1 CPU burner (red on `94cd084d`) and under 4 senders + 1 burner +
2 GIL hogs (red on `a67be362`) must never enter congested mode; a child
paused until the first put times out and resumed at once, five times, and a
child draining 2 MB/s offered a 1 MB burst from 4 tight senders (both red on
`6adfa33b`) must never enter it; a writer slowed to 30 ms per frame beside a
fast child must never enter it; a child draining 200 kB/s offered 600 kB/s
must enter it and cost the caller ≤ `TX_CONGEST_FULL_S / TX_PUT_WAIT_S` + 6
waits (red on `a67be362`); a child draining 200 kB/s in 1 MB gulps offered
600 kB/s for 6 s must enter it ONCE (red on `925d275c`: 3 episodes, 96
waits) and so must the same child offered 500-B frames at 1200 f/s (red on
`b18aae01`: 3–4 episodes, 91–104 waits; the wait bound for both is two
spans + 6, because a gulp inside the first span correctly restarts it —
measured 36 waits with and without the waited-put sampling); a child
draining 800 kB/s offered 1.2 MB/s at 500 B and at 1000 B must enter it
(red on `b18aae01`: 0 episodes, 187–203 waits, caller busy ~80 %); offers
made while offline after a SIGKILL mid-episode must all be counted and the
episode must close with a line (red on `b18aae01`: 50 refused, 0 counted,
no line); the writer clearing the span start between the caller's two reads
must not raise (red on `925d275c`: `TypeError`); a quiet gap longer than
`TX_CONGEST_FULL_S` must restart the span (red when that rule is deleted);
an episode must log its entry and its exit with
the count, and `get_interface_stats` must carry `tx_dropped` /
`tx_congested` (both red on `6adfa33b`). No wire or crypto change.

**Open on the `b18aae01` follow-up cut (built by its author; a FRESH pass is
owed before any tag):** (1) two legs are now unpinned — mutants `noreset`
(writer-side caught-up span reset deleted) and `lowwater` (caught-up
reverted to "queue at low water") both pass the stall file 21/21: the exit
duration now carries the gulp shape alone, and writable samples from waited
puts now abandon the paused-child span. BELIEVED still meaningful (a span
can survive an absorbed burst when no put waits in between); no test shows
it. (2) waited-put sampling can lengthen ENTRY: a waited put that lands
just after the child frees a page reads writable and restarts the span —
steady band (200 kB/s, 1000 B @ 600 f/s) measured 26 and 40 waits on this
cut vs 26–27 on `b18aae01`; the fast-rate test allows 31 and passed in all
six stall-file runs, so it is BELIEVED flaky, not observed flaky. (3) a
healthy `nice 10` child beside 12 burners offered a 2 MB burst congested
once in 93 interleaved runs on this cut AND once in 93 on the cut without
the waited-put sampling (1231/1487 of 2000 dropped; medians 28 vs 28): the
0.5 s span can still be outlasted by a burst into an unscheduled child.
Whether RNS ever emits > 512 KB that fast into a PipeInterface is
unmeasured. 4 KB-page kernels: MEASURED RED on the 2026-10-12 canary — see
the note below the non-author pass; mf.8 owed before any box runs this gate.

**Non-author pass on `3182bd82` (2026-10-12, Fable 5.1; tests + this note
only, code byte-identical):** (1) CLOSED — both legs were untested branches,
not dead code. `noreset`: explicit clocks + the real writer — 0.4 S of full
samples, a burst the writer absorbs, full samples again inside the quiet-gap
window — must start a new span; the mutant congests on the sum at 1.1 S.
`lowwater`: the band that tells the two caught-up rules apart is where the
pipe refills before `TX_CONGEST_FULL_S` but the queue stays under low water
past it — 500-B frames at 512–768 f/s (pipe free after a dump = 256 frames,
low water at 384); measured at 600 f/s into a 200 kB/s gulp child, the
low-water rule closed the episode twice in 10 s about a child 1.3 s behind
(111 waits), queue-EMPTY-and-pipe-writable held it (30–39 waits). Each test
kills only its own mutant; the whole matrix (6 mutants) re-run, all killed.
(2) The bound-31 flake is the TEST: stall file ×20 on this cut 20/20 green,
one 39 seen (on a mutant tree; author: one 40); 39 ≈ one span + a restarted
partial span, the mechanism the gulp tests already price at two spans. The
two fast-rate tests now use the same two-span bound; F2 unchanged. (3) No
double count: stall → crash → respawn in one episode, `offered − written −
dropped = 1` on both runs — the frame in the writer's hand when the pipe
broke, lost uncounted (≤ 1 per crash), never negative. (4) Real packets: a
private instance (`instance_name = mf7review`, 400-B PLAIN packets at
1200 f/s into a 200 kB/s gulp child) — entry line at 0.52 s, `rnstatus`
`Dropped : 3273 frame(s) (child behind, dropping now)`, SIGKILL mid-episode
→ `respawned mid-episode; 3303 frame(s) dropped during the episode, 3323
total` → `Reconnected pipe`, congested False. Still open, pre-existing stock
behaviour: `reconnect_pipe` never checks `detached`, so a detached interface
respawns its child forever (the test run's `can't open file` /
`ResourceWarning` noise, 150–400 lines per run). 4 KB pages: the lowwater
test pins nothing there (the queue dump over-fills a 64 KB pipe) — UNKNOWN.

**4 KB-page kernels, MEASURED 2026-10-12 (canary step, before any install;
Fable 5.1, same non-author):** this tag's own stall file run from a clone on
two Raspberry Pi 4s — moc4 (Python 3.11.2, kernel 6.12, load 0.2) and moc5
(Python 3.13.5) — **FAILED 5/23, then 4/23, then 4/23**; the other 63
`meshforge_*` tests pass. The same file is 20/20 on the Pi 5 (16 KB pages)
every pass ran on. Seven of the fleet's nine RNS boxes are 4 KB pages (every
Pi 3/4); the whole mf.6/mf.7 arc — build and six review passes — saw only
16 KB. The failures, by weight: (1) `test_child_behind_faster_than_a_page_
per_put_wait_still_congests`, both legs, every run: an 800 kB/s child **never
enters congested mode and the caller waits 4.3–4.6 s of every 5 s** (210–230
waits) — the mf.6 reader-saturation shape this release claims to cure. On a
4 KB kernel a pipe page frees every ~5 ms at that rate, so the F2 post-wait
sample reads writable and no span forms; the fix is page-size coupled where
its comment says ~200 kB/s and the fleet's boxes sit above it. (2) gulp child,
1000-B frames: 68–81 waits vs the two-span bound 56 (1.5–1.9 s of 6 s on the
caller); the 500-B leg failed once in three. (3) `test_healthy_child_burst_
drops_nothing`: 1 of 2000 dropped, every run — **mf.6's own stall file drops
the same 1 on moc4** (control run of the `1.3.8+mf.6` tag: 1 of 5 FAIL), so
this is a 4 KB property of the gate since mf.6, not an mf.7 regression; vs
stock (which blocks) it is the known trade, but the test's claim is false
there. The interpreter is ruled out (3.11 and 3.13 agree). Not installed
anywhere: the MeshForge/MeshAnchor pin bump to mf.7/mf.10 was REVERTED the
same hour (fleet == pin == mf.5/mf.8); this tag stays as history. Logs:
manager `~/.local/share/meshforge/mf7-review-tools/canary-4k-2026-10-12/`.
**mf.8 starts red-first from these three tests ON a 4 KB box** — a gate that
reads the pipe must be reasoned in bytes-per-page-interval, not in pages.

### `1.3.8+mf.6` (2026-10-10) — a PipeInterface child that stops draining costs that interface, not rnsd

Stock `PipeInterface.process_outgoing()` does a blocking `stdin.write()` on
the CALLER's thread (`Transport.transmit` ← `outbound` ← `Packet.send`). Once
the kernel pipe is full (64 KB on 4 KB-page kernels, 256 KB on the Pi 5's
16 KB pages — read it with `F_GETPIPE_SZ`, never assume) a stalled child
blocks that thread for as long as it stays stalled; bench drill 2026-10-10
(private rnsd, `share_instance = No`, child SIGSTOPped 60 s under ~20 kB/s):
sending thread blocked 47 s at `PipeInterface.py:130`, the TCP interface's
traffic went 20 kB/s → 0 on the same thread, `online` stayed True. Cure:
outbound frames go through a bounded queue (`TX_QUEUE_DEPTH` = 64) drained
by one writer thread per spawned child (the previous child's writer is
retired before the next starts and never writes a frame to a child it was
not spawned for — fresh pass F1: a parked writer took the next frame, broke on
the dead pipe and marked the LIVE respawned interface offline, 18/20);
`TX_QUEUE_DEPTH` = 256 and a full queue makes the caller wait at most
`TX_PUT_WAIT_S` (20 ms) for a slot — back-pressure for a draining child
(fresh pass F2: a 64-deep queue with no wait dropped 436 of a 500-frame burst
that stock's 256 KB pipe absorbed; now a 2000-frame burst drops 0); only
then is the frame dropped and counted (`tx_dropped`, which also counts frames
offered while STALL-offline so the recovery line is honest; frames offered
while crash/respawn-offline are not counted, as in stock), and if no write has COMPLETED for
`TX_STALL_S` (5 s) the interface goes `online = False` (rnstatus: Down,
`process_outgoing` drops) with a WARNING naming the stall and the drop
count; the first completed write after that flips it back with a second
WARNING. Detection latency is pipe capacity ÷ offered rate +
`max(TX_QUEUE_DEPTH ÷ frame rate, TX_STALL_S)` (second fresh pass, measured:
25.7 s at 20 frames/s, 7.6 s at 100 frames/s — the queue must fill before the
drop path runs the check): the interface can only see a write complete, not
the child consume, and
the check runs on the DROP path (no timer): a stalled child with no
offered traffic reads online until the next frame is offered. Not
changed: the read loop, HDLC framing, respawn on child exit, the
`panic_on_interface_error` path. Scope, stated: a child that stops
draining; a child that drains but never answers is still invisible here
(`last_read_ms` is computed and unused, as upstream). Test:
`tests/meshforge_pipe_interface_stall.py` — three cases with a real
SIGSTOPped subprocess; on `1.3.8+mf.5` the caller-blocks case FAILS (8 s
budget) and the offline case ERRORS (no `_tx_last_drain`); the healthy-child
control passes on both; the respawn and healthy-burst cases are red on
`eb192fa4` (online False after respawn; 1935 of 2000 dropped). No wire or crypto change.

### `1.3.8+mf.5` (2026-10-10) — an external interface that panics while loading costs that interface, not rnsd

Upstream wraps the exec + constructor of a user-supplied `interfaces/*.py`
module in `except Exception`. That contains an ordinary error, but not
`RNS.panic()` (`os._exit(255)`) or `SystemExit` — so one plugin took down
every interface on the shared instance, RNode leg included. Measured shape:
SMCI (Reticulum-Smart-MeshCore-Interface @5038e023fa) calls `RNS.panic()`
from `__init__` when `import meshcore` fails (its 0.1.0 changelog: "the
interface panics rnsd without it"). Cure: while THIS thread loads an external
interface (`RNS._external_interface_load` depth > 0, thread-local),
`RNS.panic()` raises `RNS.ExternalInterfacePanic` instead of exiting; the
loader also catches `SystemExit`, logs `... refused: it called RNS.panic()
while loading. Continuing without it.` at CRITICAL, and continues. A panic on
any other thread, or after the load, is still `os._exit(255)` (pinned).
Scope, stated: LOAD time, on the loading thread, via `RNS.panic()` or
`sys.exit()` only. NOT contained, each measured by the non-author review
(2026-10-10): a panic on a thread the plugin starts (during load or later),
a runtime panic after the load, a direct `os._exit()`, and `RNS.exit()`.
`RNS.exit()` at load time is worse than an exit, measured by drill (third
review): it latches `exit_called`, then `Reticulum.exit_handler()` calls
`Transport.exit_handler()`, which sets `Transport._should_run = False` and
THEN dies on `Transport.owner` (unset until `Transport.start()`). Before
that failure `Reticulum.exit_handler()` has already run
`Transport.detach_interfaces()`, so every interface loaded EARLIER in the
config is detached yet still listed in `Transport.interfaces`, and
`Identity.exit_handler()` is never reached (fifth review, measured on this
tree and on stock; `count_traffic_loop` survives, only `jobloop` dies). The
exception reaches the loader's catch-all, so rnsd keeps running — but when
`Transport.start()` then starts `jobloop`, the thread exits on its first
`_should_run` check: rnsd is alive and JOBLESS (no path-table maintenance,
no announce/retry jobs, no path-table save), and SIGTERM is a no-op (still
alive 6 s after SIGTERM; control exited). A systemd stop then ends in
SIGKILL. Identical on stock `c4f74c38` (fourth review, measured) — a
pre-existing upstream shape, not introduced here. After `Transport.start()`
(a Discovery re-synthesis) the same call is `os._exit(0)` with no refusal
logged. Not fixed; stated. A plugin that raises its own `BaseException`
subclass at load kills the process (stock too; stated). A plugin that
holds a Transport lock across its panic leaves it held (contrived; stated).
Review fixes (same entry, not yet tagged): `ExternalInterfacePanic` is a
`BaseException`, so a plugin's own `except Exception` cannot swallow a panic
and load as healthy (F2); and a refusal now UNREGISTERS and detaches an
interface that `interface_post_init` had already added before `final_init()`
failed, instead of leaving a dead entry while the log says it is gone (F1).
That detach is bounded by `RNS_DETACH_TIMEOUT` on a daemon thread: a plugin
whose `detach()` never returns costs a WARNING, not an rnsd that hangs at
startup (systemd restarts a crash, not a hang).
Second review fixes (fresh non-author pass 2026-10-10): the refused
interface's `detach()` runs with the guard ON, so a `panic()` there is
contained instead of `os._exit(255)` from the cleanup thread (F1), and any
`BaseException` it raises (`SystemExit`, `KeyboardInterrupt`) is logged
`Detaching refused interface ... failed: <Type>` instead of vanishing (F2);
`RNS_DETACH_TIMEOUT` must be a finite number >= 0 — `inf`/`nan`/negative/
unparseable fall back to 5 s with a WARNING at `Transport.start()` (inf/nan
used to raise inside the refusal handler and exit 255; an unparseable value
raised at `import RNS` for every program) (F3); and `RNS.panic()` records
that it was called, so a plugin whose BARE `except:` swallows the raise is
still refused, logged `it swallowed an RNS.panic()` (F4). The check runs
right after construction, BEFORE `interface_post_init` registers the
interface and runs `final_init()` (third review), and again after it, for a
panic swallowed inside `final_init()`. The `RNS_DETACH_TIMEOUT` WARNING is
logged once `Transport.start()` runs, i.e. after the interfaces have loaded.
"Refused" means unregistered from Transport, NOT inert: threads the plugin
started keep running (and can still feed `Transport.inbound`) until its own
`detach()` stops them, and the loader removes only what IT registered — a
sub-interface the plugin registered itself survives, and a `bootstrap_only`
plugin's config stays in `bootstrap_configs`, so Discovery may re-synthesise
it (and re-refuse it, CRITICAL each time) when no discovered interface is up.
Fourth review fixes (fresh non-author pass 2026-10-10): the refusal log
branches on the recorded flag, not on which exception reached the loader,
so a plugin that swallowed its panic and then raised something ordinary is
logged `it swallowed an RNS.panic() while loading, then raised <Type>`, and
one that swallowed it at import time without defining `interface_class` is
logged as a swallowed panic rather than as the `KeyError` (F1); and the
flag is also checked right after `exec`, so a panic swallowed at import
time is refused BEFORE the constructor runs (F3).
Test: `tests/meshforge_external_interface_panic.py` — 18 cases at this
entry; on the unpatched `1.3.8+mf.4` tree 16 fail (17 failure records, the
inf/nan subtests count twice; rc 255 / rc 3 / `REGISTERED 1` / missing
witness) and the two controls pass on both trees — measured, not carried.
Also fixed: `tests/meshforge_detach_timeout.py` parked non-daemon detach
threads forever, so a one-process run of the `meshforge_*` files never
exited (now released in `tearDown`; all pass and exit 0 — 63 in one process
at this entry, `tests.all` 36 OK). Live drill
2026-10-10 (real rnsd, private configdir, `share_instance = No`, TCP server
iface + the SMCI-shaped plugin), run twice: patched alive, TCP listening and
accepting, refusal logged; unpatched `1.3.8+mf.4` control exit 255, TCP never
up. Run rnsd with `PYTHONUNBUFFERED=1` when its stdout is a file — every exit
path is `os._exit()`, which drops buffered log lines. No wire or crypto
change. Upstream: not checked (ships off-GitHub); offer-back row owed.

### `1.3.8+mf.4` (2026-10-01) — join-only never binds `@rns`, and a refusal can be retried in-process

`__init__` latches `Reticulum.__instance` on its first line, and the
`require_shared_instance` refusal raised without clearing it, so the same
process could never construct again: `OSError("Attempt to reinitialise
Reticulum")`. Worse for a guarded caller, `get_instance()` then returned the
refused, half-built object as if it were live. Cure: clear the latch before
the `SystemError` (the refusal happens before `Transport.start`, signal
handlers and `atexit`, and mf.3 already released the listener), and set a
class marker `MF_REQUIRE_SHARED_RETRYABLE = True` that callers feature-detect.
Test: `tests/meshforge_require_shared_retry.py` (fails on the unpatched tree
with "refusal left a half-built singleton"; then joins a real host on retry).
Consumer: MeshForge `open_reticulum` constructs join-only once its probe has
decided to join rnsd (#69 check-then-construct race). No wire or crypto change.

Second half, from the review pair (both SHIP-WITH-NOTES; one MEASURED it):
join-only still BOUND the listener before detaching, so under three refusing
clients a starting host lost its bind and then failed to connect, ending
standalone with no `@rns` in 7 of 100 trials (11 of 100 in our own control
run of the same script). Now `require_shared_instance=True` skips the bind
entirely and goes straight to the client branch, a join-only client does not
mark itself `wanted_host`, and the refusal fires whenever join-only did not
actually JOIN (upstream tested `is_shared_instance` only, so a join-only
process whose bind and connect both failed ran standalone). Same script on
this build: 0 of 100.

### `1.3.8+mf.3` (2026-10-01) — a refused `require_shared_instance` client releases `@rns`

`Reticulum(require_shared_instance=True)` with no shared instance running
binds `LocalServerInterface` on `@rns/<instance>`, calls `detach()`, then
raises `SystemError`. `LocalServerInterface` had no `detach()` of its own: it
inherited `Interface.detach()`, a no-op, so the listener stayed bound and
accepting for the rest of the process's life. Measured 2026-10-01 in a
private network namespace: the name stayed bound for 8 s after the refusal,
and a host started 3 s in came up `shared_instance=False,
connected_to_shared=True`, attached to a refused process with no RPC socket.
That is the #69 squatter, produced by the flag meant to refuse it. Found while
checking MeshMonitor's attach mode (research M3).

Cure: `LocalServerInterface.detach()` deregisters its epoll listener and
closes it (an abstract unix name is freed only on close; `shutdown()` alone,
as `BackboneInterface.detach()` does, would leave it bound), or shuts down
and closes the `ThreadingTCPServer` on the non-epoll path. The
`listener_filenos` entry is left in place: a closed socket's `fileno()` is -1,
so the epoll loop's guard skips it and no thread mutates the dict under
another's iteration. Teardown is unchanged in effect: `deregister_listeners()`
already closed this socket right after `detach()`. Test:
`tests/meshforge_require_shared_release.py` (fails on the unpatched tree with
the name still bound). Local socket handling only; no wire or crypto change.

Why it matters on the fleet: every LOCAL `rnstatus` run passes
`require_shared_instance=True` (`rnstatus.py:159-160`, `715-716`), and
MeshForge's probes run `rnstatus` routinely. While rnsd is down (a restart, a
boot), each such run held `@rns` until it exited. Reviewed before tagging by
two independent readers (SHIP-WITH-NOTES, both): one confirmed the fix also
releases the TCP (`shared_instance_type = tcp`) and non-epoll listeners.

Also in this release: `BackboneInterface`'s accept handler was a bare
`except:` that logged an unbound `e`, so a failed `accept()` (e.g. racing a
listener close) raised UnboundLocalError and killed the epoll I/O thread for
every Backbone/Local interface in the process. Found independently by both
reviewers; now `except Exception as e:`. Test:
`tests/meshforge_backbone_accept_except.py`.

### `1.3.8+mf.2` (2026-10-01) — `interface_mode = gateway` no longer crashes rnsd

`_synthesize_interface`'s `interface_mode` branch tested `c["mode"]` for the
`gateway`/`gw` and `internal` values, so a stanza that set only
`interface_mode = gateway` raised `KeyError('mode')`. That happens before
the method's `try`, so the error escapes the `Reticulum()` constructor and
rnsd fails at startup. `mode = gateway` was unaffected; every other
`interface_mode` value already read the right key. Found while writing the
interface-mode guidance for MeshForge's templates (research R9).

Cure: read `c["interface_mode"]` in those two branches. Test:
`tests/meshforge_interface_mode_key.py` (fails with KeyError on the
unpatched tree, and checks that both keys agree for every mode). Config
parsing only; no wire or crypto change.

### `1.3.8+mf.1` (2026-09-26) — AutoInterface survives a tentative link-local

rnsd exited 255 at boot when it started inside the IPv6 DAD window: the
fe80 address was still `tentative`, the unicast discovery bind got
`EADDRNOTAVAIL`, `__init__` logged "skipping it" but left the interface
adopted, and `final_init()` re-bound it unguarded. Seen on moc5 (2026-09-19,
where the 5 s dead window let two lab daemons squat `@rns`, 42 min without
RNS) and VolcanoAI (2026-09-26). `network-online.target` does not cover it:
NetworkManager reports online before DAD completes.

Cure: `_bind_when_ready()` retries EADDRNOTAVAIL only, bounded at 10 s, and
logs at NOTICE so a reboot drill can see the path ran; a failed interface is
un-adopted so "skipping it" is true. Test: `tests/meshforge_autointerface_dad.py`
(the rollback cases fail on the unpatched tree). No wire or crypto change.
