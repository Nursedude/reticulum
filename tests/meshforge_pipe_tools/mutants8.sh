#!/bin/bash
# mf.8/mf.9/mf.10 mutant matrix, PATTERN-anchored (the mf.7 script used line numbers).
# mf.9 carries the author's 11 (nothrottled re-anchored on the new call), the
# non-author pass's 6 on 9120f760, and 12 aimed at the mf.9 changes.
# mf.10 carries all 29 (noidle/dropsonly re-anchored; quietexit re-anchored, see
# below), adds the c3c52232 pass's 5 (slowpoll/endspannoop re-anchored,
# stickyhold re-anchored) and 6 aimed at the mf.10 exit accounting: 40.
# Usage: mutants8.sh <tree> <outdir> [python]   — runs the whole stall file per mutant.
# FAILFAST=1 stops each run at its first failure (a killed mutant then shows ONE
# killing test, not all of them; a survivor still runs the whole file).
TREE=${1:?tree}; SP=${2:?outdir}; PY=${3:-python3}; mkdir -p "$SP"; OUT=$SP/mutants.out; : > "$OUT"
declare -A SEDS=(
  [span0]='s/if full_for >= PipeInterface.TX_CONGEST_FULL_S and not self._tx_congested:/if full_for >= 0 and not self._tx_congested:/'
  [never]='s/return buffered >= cap \* PipeInterface.TX_PIPE_FULL_FRACTION/return False/'
  [always]='s/return buffered >= cap \* PipeInterface.TX_PIPE_FULL_FRACTION/return True/'
  [selectpoll]='s/return buffered >= cap \* PipeInterface.TX_PIPE_FULL_FRACTION/return not select.select([], [fd], [], 0)[1]/'
  [nogap]='s/if since is None or now - last > PipeInterface.TX_CONGEST_FULL_S:/if since is None:/'
  [noreset]='s/if self._tx_full_since is not None and self._child_caught_up():/if False:/'
  [lowwater]='s/return self._txq.qsize() == 0 and not self._pipe_full()/return self._txq.qsize() <= PipeInterface.TX_LOW_WATER/'
  [countweighted]='s/gap = now - last if now > last else 0.0/gap = PipeInterface.TX_PUT_WAIT_S/'
  [noroom]='s/if self._tx_room_time > self._tx_full_time:/if False:/'
  [anyroom]='s/if self._tx_room_time > self._tx_full_time:/if not backed_up:/'
  [nothrottled]='s/if throttled or now - t >= PipeInterface.TX_PUT_WAIT_S \/ 2: self._note_pipe_full(now, now - t)/if now - t >= PipeInterface.TX_PUT_WAIT_S \/ 2: self._note_pipe_full(now, now - t)/'
  # non-author pass on 9120f760 (2026-10-12): 5 of these 6 survived there
  [gapge]='s/if since is None or now - last > PipeInterface.TX_CONGEST_FULL_S:/if since is None or now - last >= PipeInterface.TX_CONGEST_FULL_S:/'
  [roomge]='s/if self._tx_room_time > self._tx_full_time:/if self._tx_room_time >= self._tx_full_time:/'
  [frac25]='s/TX_PIPE_FULL_FRACTION = 0.5/TX_PIPE_FULL_FRACTION = 0.25/'
  [capfail]='s/cap = fcntl.fcntl(fd, PipeInterface.F_GETPIPE_SZ)/cap = 0/'
  [noclamp]='s/gap = now - last if now > last else 0.0/gap = now - last/'
  [wallclock]='s/time\.monotonic()/time.time()/g'
  # mf.9: aimed at the F1/F3/F4/F5 changes
  [rawgap]='s/credit = min(gap, waited)/credit = gap/'
  [noidle]='s/if caught_up: self._end_span(now)/pass/'
  [frac75]='s/TX_PIPE_FULL_FRACTION = 0.5/TX_PIPE_FULL_FRACTION = 0.75/'
  [everyoffer]='s/else: self._tx_clear_full += credit/else: self._tx_clear_since = None; return False/'
  [dropsonly]='s/if self._tx_clear_room > self._tx_clear_full and observed_room:/if True:/'
  [nodropexit]='s/if dropped or self._txq.qsize() > PipeInterface.TX_LOW_WATER:/if self._txq.qsize() > PipeInterface.TX_LOW_WATER:/'
  [nolwexit]='s/if dropped or self._txq.qsize() > PipeInterface.TX_LOW_WATER:/if dropped:/'
  # quietexit was "an offer gap does not restart the exit window" on mf.9's
  # gap line. In mf.10 that line is gone (no gap restart IS the design), so it
  # is re-anchored to its descendant: unobserved quiet counted as caught up
  # (the coverage half of the exit rule removed).
  [quietexit]='s/observed_room = self._tx_clear_room >= PipeInterface.TX_CONGEST_FULL_S \/ 2/observed_room = True/'
  [rawexit]='s/credit = min(gap, PipeInterface.TX_PUT_WAIT_S)/credit = gap/'
  [nolock]='s/with self._tx_gate_lock:/if True:/g'
  [capsilent]='s/self.tx_probe_errors += 1   # capacity unreadable/pass/'
  [capquiet]='s/if not self._pipe_cap_warned:/if False:/'
  # non-author pass on c3c52232 (2026-10-12): waitfloor20 survived there,
  # stickyhold was killed only by the test that pinned the defect
  [waitedonly]='s/credit = min(gap, waited)/credit = waited/'
  [slowpoll]='s/data = self._txq.get(timeout=PipeInterface.TX_PUT_WAIT_S if watching else 0.5)/data = self._txq.get(timeout=0.5)/'
  [endspannoop]='s/if self._tx_full_since is not None and self._tx_full_since <= observed_at: self._tx_full_since = None/pass/'
  [waitfloor20]='s/if throttled or now - t >= PipeInterface.TX_PUT_WAIT_S \/ 2: self._note_pipe_full(now, now - t)/if throttled or now - t >= PipeInterface.TX_PUT_WAIT_S \/ 20: self._note_pipe_full(now, now - t)/'
  # stickyhold on mf.9 disabled the gap restart (> 3600 s): the design now.
  # Re-anchored to what makes the flag sticky in mf.10: the writer's poll is
  # no longer an exit observer (the flag holds through quiet).
  [stickyhold]='s/if self._tx_congested and self._caught_up_for(now, caught_up): self._end_episode()/pass/'
  # mf.10: aimed at the exit accounting
  [pollbehind]='s/if self._tx_congested and self._caught_up_for(now, caught_up): self._end_episode()/if self._tx_congested and self._caught_up_for(now, False): self._end_episode()/'
  [offeronlyexit]='s/if self._tx_congested and self._caught_up_for(now, caught_up): self._end_episode()/if self._tx_congested: self._caught_up_for(now, caught_up)/'
  [gaprestart]='s/^            if since is None:$/            if since is None or now - last > PipeInterface.TX_PUT_WAIT_S:/'
  [spanonlypoll]='s/watching = self._tx_full_since is not None or self._tx_congested/watching = self._tx_full_since is not None/'
  [endspanstale]='s/if self._tx_full_since is not None and self._tx_full_since <= observed_at: self._tx_full_since = None/if self._tx_full_since is not None: self._tx_full_since = None/'
  [offlineobs]='s/^        if not self.online: return$/        pass/'
)
ALL="span0 never always selectpoll nogap noreset lowwater countweighted noroom anyroom nothrottled gapge roomge frac25 capfail noclamp wallclock rawgap noidle frac75 everyoffer dropsonly nodropexit nolwexit quietexit rawexit nolock capsilent capquiet waitedonly slowpoll endspannoop waitfloor20 stickyhold pollbehind offeronlyexit gaprestart spanonlypoll endspanstale offlineobs"
for m in ${MUTS:-$ALL}; do
  M=$SP/mut_$m; rm -rf "$M"; mkdir -p "$M"; cp -r "$TREE/RNS" "$TREE/tests" "$M/"
  f=$M/RNS/Interfaces/PipeInterface.py; cp "$f" "$f.orig"; sed -i "${SEDS[$m]}" "$f"
  if cmp -s "$f" "$f.orig"; then echo "$m: SED DID NOT APPLY" >> "$OUT"; rm -rf "$M"; continue; fi
  (cd "$M" && PYTHONUNBUFFERED=1 PYTHONPATH="$M:${PYTHONPATH:-}" timeout 600 "$PY" -m unittest ${FAILFAST:+-f} tests.meshforge_pipe_interface_stall > "$SP/mut_$m.log" 2>&1; echo "$m rc=$? $(grep -E '^Ran|^OK|^FAILED' "$SP/mut_$m.log" | tr '\n' ' ')" >> "$OUT")
  grep -E "^(FAIL|ERROR):" "$SP/mut_$m.log" | sed 's/ (tests.*//; s/^/    /' >> "$OUT"
  rm -rf "$M"
done
echo DONE >> "$OUT"
