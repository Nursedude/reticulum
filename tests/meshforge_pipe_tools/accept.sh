#!/bin/bash
# mf.10 acceptance: EVERY known traffic shape for the PipeInterface congestion
# gate, in one run, with its controls. Run it BEFORE a change (the red baseline)
# and AFTER it, on a 4 KB-page box first (Pi 3/4: 64 KB pipe, 32 KB line).
# No RNS instance is started; every drill uses the stall test file's fixture.
#
# Usage: accept.sh <tree-under-test> <outdir> [python]
#   The drills come from THIS directory; the gate comes from <tree-under-test>,
#   so the same script measures any cut (c3c52232, 9120f760, ...).
#   Sequential; waits for loadavg < 1.0 before each run (max 120 s, logged).
#   Every run: <outdir>/<run>.log + <run>.rc. Verdict table: <outdir>/verdict.txt.
#
# Legs — HEALTHY reading / DEFECTIVE reading:
#  1 pulse     drill_pulse_x.py LOG=1 x5 (400 x 1000 B every 0.25 s into a
#              2 MB/s child). HEALTHY: 0 entered in all 5, 0 pre-pulse lines
#              with a span open over `occ=0 qsize=0`. DEFECTIVE: any entry
#              (9120f760: 1 of 5-10, 339-1,204 drops) or a span carried over
#              a drained pipe. Control: RATE=100000 (child behind) MUST enter.
#  2 tick      drill_tick.py RATE_BPS=160000: HEALTHY: closes within ~4 s of
#              the hold (<= 5 s, 1 s granularity), drops flat after. DEFECTIVE:
#              congested for the whole 10 s hold (9120f760, F4). Control
#              RATE_BPS=100000: closes, drops flat.
#  3 sticky    drill_sticky.py episode, LOW_GAP 0.7 x3: HEALTHY: the flag is
#              clear by the end of the sparse phase and the 2,000-frame burst
#              drops 0. DEFECTIVE: congested=True after ~10 s of healthy sparse
#              traffic, burst drops 1,557-1,741 (c3c52232). Controls: LOW_GAP
#              0.3 (exits, 0 drops); no episode (0 drops).
#  4 quietburst drill_shapes.py quietburst x3: episode, then NO offers for 3 s,
#              then 2,000 x 500 B. HEALTHY: flag cleared before the burst, 0
#              drops. DEFECTIVE: flag still set, the burst meets no
#              back-pressure and is dropped. Control quietctl: 0 drops.
#  5 slowgulp  drill_occ.py slowgulp 10: HEALTHY: enters once, never exits in
#              the run. DEFECTIVE: no entry, or an exit about a child behind.
#  6 hung      drill_shapes.py hung: SIGSTOP held 3 s under offers. HEALTHY:
#              enters <= 0.7 s after the queue first reads full, 0 exits while
#              stopped, exits <= 1.0 s after the child drained, clear at end.
#  7 burst     drill_occ.py burst: HEALTHY: 0 entered (the 4 KB startup drop
#              of frame 321 is not the gate). DEFECTIVE: any entry.
#  8 behind    drill_shapes.py behind (`rate 100000`, 300 kB/s offered, 8 s):
#              HEALTHY: 1 entry, 0 exits, congested at the end.
TREE=$(cd "${1:?tree}" && pwd); OUT=${2:?outdir}; PY=${3:-python3}
TOOLS=$(cd "$(dirname "$0")" && pwd); mkdir -p "$OUT"; : > "$OUT/index"
export PYTHONUNBUFFERED=1 PYTHONPATH="$TREE:${PYTHONPATH:-}"
cd "$TREE" || exit 3
quiet() { for _ in $(seq 1 60); do awk '{exit !($1 < 1.0)}' /proc/loadavg && return; sleep 2; done; }
R() { local n=$1; shift; quiet
      echo "== $n start $(date +%T) load=$(cut -d' ' -f1 /proc/loadavg)" >> "$OUT/index"
      timeout 120 "$@" > "$OUT/$n.log" 2>&1; echo $? > "$OUT/$n.rc"
      echo "== $n rc=$(cat "$OUT/$n.rc") $(date +%T)" >> "$OUT/index"; }
echo "tree=$TREE head=$(git -C "$TREE" rev-parse --short HEAD 2>/dev/null || echo none) gate_md5=$(md5sum "$TREE/RNS/Interfaces/PipeInterface.py" | cut -c1-12) pagesize=$(getconf PAGESIZE) host=$(hostname)" > "$OUT/meta"
for k in 1 2 3 4 5; do LOG=1 R pulse$k "$PY" "$TOOLS/drill_pulse_x.py" "$TREE"; done
RATE=100000 R pulse_ctl "$PY" "$TOOLS/drill_pulse_x.py" "$TREE"
RATE_BPS=160000 R tick160 "$PY" "$TOOLS/drill_tick.py" "$TREE" "$TOOLS/tick_child.py"
RATE_BPS=100000 R tick100 "$PY" "$TOOLS/drill_tick.py" "$TREE" "$TOOLS/tick_child.py"
for k in 1 2 3; do R sticky07_$k "$PY" "$TOOLS/drill_sticky.py" "$TREE" episode; done
LOW_GAP=0.3 R sticky03 "$PY" "$TOOLS/drill_sticky.py" "$TREE" episode
R stickyctl "$PY" "$TOOLS/drill_sticky.py" "$TREE" control
for k in 1 2 3; do R quietburst$k "$PY" "$TOOLS/drill_shapes.py" "$TREE" quietburst; done
R quietctl "$PY" "$TOOLS/drill_shapes.py" "$TREE" quietctl
R slowgulp "$PY" "$TOOLS/drill_occ.py" "$TREE" slowgulp 10
R hung "$PY" "$TOOLS/drill_shapes.py" "$TREE" hung
R burst "$PY" "$TOOLS/drill_occ.py" "$TREE" burst
R behind "$PY" "$TOOLS/drill_shapes.py" "$TREE" behind
"$PY" "$TOOLS/accept_verdict.py" "$OUT" > "$OUT/verdict.txt" 2>&1; echo $? > "$OUT/verdict.rc"
cat "$OUT/verdict.txt"
echo DONE >> "$OUT/index"
exit "$(cat "$OUT/verdict.rc")"
