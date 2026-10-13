#!/bin/bash
# mf.10 acceptance: EVERY known traffic shape for the PipeInterface congestion
# gate, in one run, with its controls. Run it BEFORE a change (the red baseline)
# and AFTER it, on a 4 KB-page box first (Pi 3/4: 64 KB pipe, 32 KB line).
# No RNS instance is started; every drill uses the stall test file's fixture.
#
# Usage: [LEGS="9 10 11"] accept.sh <tree-under-test> <outdir> [python]
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
# Legs 9-11 (added 2026-10-12 after the fresh pass on 8acd4d18; AXES, not
# shapes: burner count B, idle duration, frame SIZE). Burners are pure-Python
# spin threads in the test process, started BEFORE the provoke and running
# through the burst. LOAD RULE: the load is the writer's idle-poll gap median
# (when >= 5 polls were seen) else drill_gil's witness (the main thread waiting
# queue.get(timeout=TX_PUT_WAIT_S) through the quiet). A HEALTHY B=2 run under
# 100 ms is UNKNOWN (contention not reached), never GREEN; a DEFECTIVE one stays
# RED; a B=0 control at >= 100 ms is UNKNOWN. Leg: any RED run -> RED.
#  9 sticky-B2 drill_gil.py episode, B=2, LOW_GAP 0.7, QUIET_S 7, x3, with
#              PROVOKE_S 4 (under B=2 from the start 8acd4d18 often cannot enter
#              within the original 1.5 s; an unentered run is UNKNOWN). HEALTHY:
#              flag clear before the burst, burst drops < 100. DEFECTIVE: the
#              exit never fires (writer poll gap 20 ms -> 168-548 ms), burst
#              drops ~1,740 (8acd4d18, c3c52232). Controls: B=0 (exits, 0
#              drops); no episode at B=2 (0-12 drops).
# 10 idle      drill_gil.py control (no episode), QUIET_S = TX_STALL_S + 1
#              (read from the class), B=2, x5. HEALTHY: no `has not drained its
#              stdin` line, `online` never False, burst drops < 100.
#              DEFECTIVE (F2, pre-existing since mf.6): _tx_last_drain is the
#              last COMPLETED write, so the first put that waits after the idle
#              reads as a 5 s stall about a healthy child. Controls: B=0 at the
#              same idle x3; 3 s idle at B=2 x3.
# 11 smallgulp drill_smallgulp.py, gulp child 30 kB/s, offered 40 kB/s, 30 s,
#              SIZE=30 x2. HEALTHY: 1 entry, 0 exits, congested at end.
#              DEFECTIVE (F3, 4 KB pages): a full queue of < ~125 B frames fits
#              under the 32 KB line, so each gulp reads as caught up -> 8-9
#              entries/exits. Controls: `rate` child, same mean; SIZE=500 for
#              40 s (its 64 KB pipe + 256 x 502 B queue fill at the 10 kB/s
#              excess in ~19 s; 30 s would leave too little run after entry).
# Select legs with LEGS="9 10 11" (default all 11); the verdict judges only those.
TREE=$(cd "${1:?tree}" && pwd); OUT=${2:?outdir}; PY=${3:-python3}
TOOLS=$(cd "$(dirname "$0")" && pwd); mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd); : > "$OUT/index"   # absolute: the runs cd into the tree
export PYTHONUNBUFFERED=1 PYTHONPATH="$TREE:${PYTHONPATH:-}"
cd "$TREE" || exit 3
quiet() { for _ in $(seq 1 60); do awk '{exit !($1 < 1.0)}' /proc/loadavg && return; sleep 2; done; }
R() { local n=$1; shift; quiet
      echo "== $n start $(date +%T) load=$(cut -d' ' -f1 /proc/loadavg)" >> "$OUT/index"
      timeout 120 "$@" > "$OUT/$n.log" 2>&1; echo $? > "$OUT/$n.rc"
      echo "== $n rc=$(cat "$OUT/$n.rc") $(date +%T)" >> "$OUT/index"; }
LEGS=${LEGS:-1 2 3 4 5 6 7 8 9 10 11}
on() { case " $LEGS " in *" $1 "*) return 0;; esac; return 1; }
PIPESZ=$("$PY" -c 'import os,fcntl;r,w=os.pipe();print(fcntl.fcntl(w,1032))' 2>/dev/null || echo unknown)
echo "tree=$TREE head=$(git -C "$TREE" rev-parse --short HEAD 2>/dev/null || echo none) gate_md5=$(md5sum "$TREE/RNS/Interfaces/PipeInterface.py" | cut -c1-12) pagesize=$(getconf PAGESIZE) pipe_sz=$PIPESZ python=$("$PY" -c 'import platform;print(platform.python_version())') nproc=$(nproc) host=$(hostname) legs=$(echo $LEGS | tr ' ' ',')" > "$OUT/meta"
echo "WHERE: $(cat "$OUT/meta") loadavg_at_start=$(cut -d' ' -f1-3 /proc/loadavg)" >> "$OUT/index"
if on 1; then for k in 1 2 3 4 5; do LOG=1 R pulse$k "$PY" "$TOOLS/drill_pulse_x.py" "$TREE"; done
RATE=100000 R pulse_ctl "$PY" "$TOOLS/drill_pulse_x.py" "$TREE"; fi
on 2 && RATE_BPS=160000 R tick160 "$PY" "$TOOLS/drill_tick.py" "$TREE" "$TOOLS/tick_child.py"
on 2 && RATE_BPS=100000 R tick100 "$PY" "$TOOLS/drill_tick.py" "$TREE" "$TOOLS/tick_child.py"
if on 3; then for k in 1 2 3; do R sticky07_$k "$PY" "$TOOLS/drill_sticky.py" "$TREE" episode; done
LOW_GAP=0.3 R sticky03 "$PY" "$TOOLS/drill_sticky.py" "$TREE" episode
R stickyctl "$PY" "$TOOLS/drill_sticky.py" "$TREE" control; fi
if on 4; then for k in 1 2 3; do R quietburst$k "$PY" "$TOOLS/drill_shapes.py" "$TREE" quietburst; done
R quietctl "$PY" "$TOOLS/drill_shapes.py" "$TREE" quietctl; fi
on 5 && R slowgulp "$PY" "$TOOLS/drill_occ.py" "$TREE" slowgulp 10
on 6 && R hung "$PY" "$TOOLS/drill_shapes.py" "$TREE" hung
on 7 && R burst "$PY" "$TOOLS/drill_occ.py" "$TREE" burst
on 8 && R behind "$PY" "$TOOLS/drill_shapes.py" "$TREE" behind
G="$TOOLS/drill_gil.py"
if on 9; then for k in 1 2 3; do PROVOKE_S=4 BURNERS=2 LOW_GAP=0.7 QUIET_S=7 R gil9_ep_b2_$k "$PY" "$G" "$TREE" episode; done
PROVOKE_S=4 BURNERS=0 LOW_GAP=0.7 QUIET_S=7 R gil9_ep_b0 "$PY" "$G" "$TREE" episode
BURNERS=2 LOW_GAP=0.7 QUIET_S=7 R gil9_ctl_b2 "$PY" "$G" "$TREE" control; fi
if on 10; then for k in 1 2 3 4 5; do BURNERS=2 QUIET_S=stall+1 R gil10_idle_b2_$k "$PY" "$G" "$TREE" control; done
for k in 1 2 3; do BURNERS=0 QUIET_S=stall+1 R gil10_idle_b0_$k "$PY" "$G" "$TREE" control; done
for k in 1 2 3; do BURNERS=2 QUIET_S=3 R gil10_q3_b2_$k "$PY" "$G" "$TREE" control; done; fi
if on 11; then SG="$TOOLS/drill_smallgulp.py"
for k in 1 2; do SIZE=30 FPS=auto SECS=30 R sg11_30_$k "$PY" "$SG" "$TREE"; done
SIZE=30 FPS=auto SECS=30 CHILDMODE=rate R sg11_30_rate "$PY" "$SG" "$TREE"
SIZE=500 FPS=auto SECS=40 R sg11_500 "$PY" "$SG" "$TREE"; fi
"$PY" "$TOOLS/accept_verdict.py" "$OUT" > "$OUT/verdict.txt" 2>&1; echo $? > "$OUT/verdict.rc"
cat "$OUT/verdict.txt"
echo DONE >> "$OUT/index"
exit "$(cat "$OUT/verdict.rc")"
