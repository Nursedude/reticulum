#!/bin/bash
# mf.8 mutant matrix, PATTERN-anchored (the mf.7 script used line numbers).
# Usage: mutants8.sh <tree> <outdir> [python]   — runs the whole stall file per mutant.
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
  [nothrottled]='s/if throttled or now - t >= PipeInterface.TX_PUT_WAIT_S \/ 2: self._note_pipe_full(now)/if now - t >= PipeInterface.TX_PUT_WAIT_S \/ 2: self._note_pipe_full(now)/'
)
for m in ${MUTS:-span0 never always selectpoll nogap noreset lowwater countweighted noroom anyroom nothrottled}; do
  M=$SP/mut_$m; rm -rf "$M"; mkdir -p "$M"; cp -r "$TREE/RNS" "$TREE/tests" "$M/"
  f=$M/RNS/Interfaces/PipeInterface.py; cp "$f" "$f.orig"; sed -i "${SEDS[$m]}" "$f"
  if cmp -s "$f" "$f.orig"; then echo "$m: SED DID NOT APPLY" >> "$OUT"; rm -rf "$M"; continue; fi
  (cd "$M" && PYTHONUNBUFFERED=1 PYTHONPATH="$M:${PYTHONPATH:-}" timeout 400 "$PY" -m unittest tests.meshforge_pipe_interface_stall > "$SP/mut_$m.log" 2>&1; echo "$m rc=$? $(grep -E '^Ran|^OK|^FAILED' "$SP/mut_$m.log" | tr '\n' ' ')" >> "$OUT")
  grep -E "^(FAIL|ERROR):" "$SP/mut_$m.log" | sed 's/ (tests.*//; s/^/    /' >> "$OUT"
  rm -rf "$M"
done
echo DONE >> "$OUT"
