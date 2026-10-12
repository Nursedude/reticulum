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

### `1.3.8+mf.7` (UNRELEASED) — a PipeInterface child that is behind costs a forwarding reader one wait per congestion episode, and stops flapping

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
unmeasured. 4 KB-page kernels remain unchecked (no box).

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
