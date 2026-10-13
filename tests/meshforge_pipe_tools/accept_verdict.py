"""Verdict table for accept.sh: reads <outdir>/<run>.log + .rc and applies the
HEALTHY readings named in accept.sh's header. Only the legs named in
meta `legs=` are judged (default all 11). Exit 0 = every leg GREEN, 1 = a
leg RED, 2 = a run missing or unparseable (UNKNOWN is never a pass)."""
import os, re, sys
OUT = sys.argv[1]
def log(n):
    p = os.path.join(OUT, n + ".log")
    return open(p).read() if os.path.exists(p) else None
def rc(n):
    p = os.path.join(OUT, n + ".rc")
    return open(p).read().strip() if os.path.exists(p) else "missing"
def kv(line):
    return dict(re.findall(r"(\w+)=(\[[^\]]*\]|\S+)", line))
def result(n):
    t = log(n)
    if t is None: return None
    ls = [l for l in t.splitlines() if l.startswith("RESULT")]
    return kv(ls[-1]) if ls else None
_meta = open(os.path.join(OUT, "meta")).read() if os.path.exists(os.path.join(OUT, "meta")) else ""
_m = re.search(r"legs=([\d,]+)", _meta)
LEGS = {int(x) for x in _m.group(1).split(",")} if _m else set(range(1, 9))   # pre-legs-9-11 outdirs
rows = []; unknown = False
def row(leg, ok, detail):
    global unknown
    if int(leg.split()[0]) not in LEGS: return   # not selected for this run
    if ok is None: unknown = True
    rows.append((leg, "UNKNOWN" if ok is None else ("GREEN" if ok else "RED"), detail))

# 1 pulse
det = []; ok = True
for k in range(1, 6):
    n = f"pulse{k}"; t = log(n)
    if t is None or rc(n) != "0": ok = None; det.append(f"{n}:rc={rc(n)}"); continue
    m = re.search(r"pulsex: entered_at=(\[[^\]]*\]).*dropped=(\d+)", t)
    if not m: ok = None; det.append(f"{n}:unparsed"); continue
    carried = sum(1 for l in t.splitlines() if l.strip().startswith("pre ") and " occ=0 qsize=0 " in l and "full_since=None" not in l)
    ent = m.group(1)
    if ent != "[]" or carried: ok = ok and False
    det.append(f"{n}:entered={ent},drops={m.group(2)},span_over_drained={carried}")
t = log("pulse_ctl"); m = t and re.search(r"pulsex: entered_at=(\[[^\]]*\])", t)
ctl = bool(m) and m.group(1) != "[]"
if not m: ok = None
elif not ctl and ok is not None: ok = False
det.append(f"ctl(RATE=100000):entered={m.group(1) if m else '?'}")
row("1 pulse x5 + ctl", ok, "; ".join(det))

# 2 tick
def tick(n, bound):
    t = log(n)
    if t is None or rc(n) != "0": return None, f"{n}:rc={rc(n)}"
    prov = [kv(l) for l in t.splitlines() if "[prov]" in l]
    main = [kv(l) for l in t.splitlines() if "[main]" in l]
    fin = re.search(r"tick: congested_at_end=(\w+) dropped=(\d+)", t)
    if not main or not fin: return None, f"{n}:unparsed"
    entered = any(p.get("congested") == "True" for p in prov) or any(x.get("congested") == "True" for x in main)
    ex = next((x for x in main if x.get("congested") == "False"), None)
    if ex is None: return False, f"{n}:entered={entered},no exit in the hold,final_drops={fin.group(2)}"
    into = int(ex["sec"]) + 1 - 3
    flat = ex["dropped"] == fin.group(2)
    return (entered and into <= bound and flat), f"{n}:entered={entered},closed<= {into}s into hold,drops at close={ex['dropped']} final={fin.group(2)}"
a, da = tick("tick160", 5); b, db = tick("tick100", 5)
row("2 tick 160k + ctl 100k", None if a is None or b is None else (a and b), "; ".join([da, db]))

# 3 sticky
det = []; ok = True
for n in ("sticky07_1", "sticky07_2", "sticky07_3", "sticky03", "stickyctl"):
    t = log(n)
    if t is None or rc(n) != "0": ok = None; det.append(f"{n}:rc={rc(n)}"); continue
    lp = re.search(r"low-rate phase:.*congested=(\w+) drops_in_phase=(\d+)", t)
    pv = re.search(r"provoke: congested=(\w+)", t)
    r = result(n)
    if not lp or not r: ok = None; det.append(f"{n}:unparsed"); continue
    good = r["burst_drops"] == "0" and lp.group(1) == "False"
    if n != "stickyctl": good = good and pv is not None and pv.group(1) == "True"
    if not good and ok is not None: ok = False
    det.append(f"{n}:provoked={pv.group(1) if pv else '-'},after_sparse_congested={lp.group(1)},burst_drops={r['burst_drops']}")
row("3 sticky 0.7s x3 + 0.3s + no-episode", ok, "; ".join(det))

# 4 quiet-then-burst
det = []; ok = True
for n in ("quietburst1", "quietburst2", "quietburst3", "quietctl"):
    r = result(n)
    if r is None or rc(n) != "0": ok = None; det.append(f"{n}:rc={rc(n)}"); continue
    good = r["burst_drops"] == "0" and r["congested_before_burst"] == "False"
    if n != "quietctl": good = good and r["provoked"] == "True"
    if not good and ok is not None: ok = False
    det.append(f"{n}:provoked={r['provoked']},congested_before_burst={r['congested_before_burst']},burst_drops={r['burst_drops']}")
row("4 quiet-then-burst x3 + ctl", ok, "; ".join(det))

# 5 slowgulp
r = result("slowgulp")
row("5 slowgulp stays congested", None if r is None or rc("slowgulp") != "0" else (r["entered"] == "1" and r["congested_at_run_end"] == "True"),
    "rc=%s %s" % (rc("slowgulp"), "" if r is None else f"entered={r['entered']},exits_during_run={r['exits_during_run']},congested_at_run_end={r['congested_at_run_end']},dropped={r['dropped']}"))

# 6 hung
r = result("hung")
def f(x):
    try: return float(x)
    except Exception: return None
if r is None or rc("hung") != "0": row("6 hung (SIGSTOP) child", None, f"rc={rc('hung')}")
else:
    e = f(r["entry_after_queue_full"]); x = f(r["exit_after_drained"])
    good = r["entries"] == "1" and e is not None and e <= 0.7 and r["exits_while_stopped"] == "0" and x is not None and x <= 1.0 and r["congested_end"] == "False"
    row("6 hung (SIGSTOP) child", good, f"entries={r['entries']},entry_after_queue_full={r['entry_after_queue_full']}s,entry_after_stop={r['entry_after_stop']}s,exits_while_stopped={r['exits_while_stopped']},drained_after_cont={r['drained_after_cont']}s,exit_after_drained={r['exit_after_drained']}s,congested_end={r['congested_end']}")

# 7 burst
r = result("burst")
row("7 healthy burst 0 entered", None if r is None or rc("burst") != "0" else r["entered"] == "0",
    "rc=%s %s" % (rc("burst"), "" if r is None else f"entered={r['entered']},dropped={r['dropped']}"))

# 8 behind
r = result("behind")
row("8 behind rate 100000 stays", None if r is None or rc("behind") != "0" else (r["entries"] == "1" and r["exits"] == "0" and r["congested_end"] == "True"),
    "rc=%s %s" % (rc("behind"), "" if r is None else f"entries={r['entries']},exits={r['exits']},congested_end={r['congested_end']},dropped={r['dropped']}"))

# --- legs 9-11 (added after the fresh pass on 8acd4d18, 2026-10-12; arc paused) ---
# LOAD rule: a leg that claims to run under CPU contention must show the
# contention. The witness is drill_gil.py's wake_gap_median_ms (the main thread
# sleeping in TX_PUT_WAIT_S steps through the quiet -- tree-independent; the
# writer's own poll witness exists only on trees with _writer_observes and only
# while the flag is set; it is preferred when >= 5 polls were seen). A HEALTHY
# B=2 run under LOAD_MIN_MS is UNKNOWN (contention not reached), never GREEN;
# a DEFECTIVE one stays RED (on moc4 F1 reproduced at writer poll gaps of
# 41-59 ms, so 100 ms is a floor for trusting a green, not the defect's edge).
# A B=0 control at or over the floor is UNKNOWN (the control was loaded). An episode run that never
# entered is UNKNOWN (the shape was not reached), never RED or GREEN.
LOAD_MIN_MS = 100.0
BURST_DROP_MAX = 100   # no-episode B=2 controls read 0-12 on moc4 (fresh pass)
def gil_runs(names, healthy, leg):
    # per-run state: R (defect read), U (unobservable / not reached), G.
    # Leg: any R -> RED (a reproduced defect is proven); else any U -> UNKNOWN.
    det = []; st = []
    for n in names:
        r = result(n)
        if r is None or rc(n) != "0": st.append("U"); det.append(f"{n}:rc={rc(n)}"); continue
        wg = float(r.get("wake_gap_median_ms", "-1")); b = int(r["B"])
        pg = float(r.get("poll_gap_median_ms", "-1")); np_ = int(r.get("polls_in_quiet", "0"))
        # the writer's own gap when it was observable (>= 5 polls), else the witness
        lg, src = (pg, "poll") if np_ >= 5 else (wg, "wake")
        load = f"load={lg:.0f}ms({src}),wake={wg:.0f},poll={pg:.0f}"
        if b == 0 and lg >= LOAD_MIN_MS:
            st.append("U"); det.append(f"{n}:UNKNOWN(B=0 control loaded, {load})"); continue
        if "_ep_" in n and r["provoked"] != "True":
            st.append("U"); det.append(f"{n}:UNKNOWN(episode not provoked, {load})"); continue
        good = healthy(n, r)
        tag = "G" if good else "R"
        if good and b >= 2 and lg < LOAD_MIN_MS:
            # healthy at a load under the floor measured nothing: never GREEN.
            # A DEFECTIVE reading under the floor stays RED (the defect showed
            # at less load than claimed -- a reading, not a vacuum).
            tag = "U"
        st.append(tag)
        det.append(f"{n}:{tag},B={b},{load},provoked={r['provoked']},cbb={r['congested_before_burst']},burst_drops={r['burst_drops']},stall_lines={r.get('stall_lines')},offline={r.get('offline_events')}")
    row(leg, False if "R" in st else (None if "U" in st else True), "; ".join(det))
def h9(n, r):
    return r["congested_before_burst"] == "False" and int(r["burst_drops"]) < BURST_DROP_MAX
gil_runs(["gil9_ep_b2_1", "gil9_ep_b2_2", "gil9_ep_b2_3", "gil9_ep_b0", "gil9_ctl_b2"], h9,
         "9 sticky under CPU contention B=2 x3 + B=0 + no-episode B=2")
def h10(n, r):
    return (r.get("stall_lines") == "0" and r.get("offline_events") == "0"
            and r["congested_before_burst"] == "False" and int(r["burst_drops"]) < BURST_DROP_MAX)
gil_runs([f"gil10_idle_b2_{k}" for k in range(1, 6)] + [f"gil10_idle_b0_{k}" for k in range(1, 4)]
         + [f"gil10_q3_b2_{k}" for k in range(1, 4)], h10,
         "10 idle TX_STALL_S+1 then burst B=2 x5 + B=0 x3 + 3s B=2 x3")
det = []; ok = True
for n in ("sg11_30_1", "sg11_30_2", "sg11_30_rate", "sg11_500"):
    r = result(n)
    if r is None or rc(n) != "0": ok = None; det.append(f"{n}:rc={rc(n)}"); continue
    good = r["entries"] == "1" and r["exits"] == "0" and r["congested_end"] == "True"
    if not good and ok is not None: ok = False
    det.append(f"{n}:SIZE={r['SIZE']},CM={r['CM']},entries={r['entries']},exits={r['exits']},duty={r['duty']},congested_end={r['congested_end']}")
row("11 gulp SIZE=30 x2 + rate ctl + SIZE=500", ok, "; ".join(det))

meta = open(os.path.join(OUT, "meta")).read().strip() if os.path.exists(os.path.join(OUT, "meta")) else "meta missing"
print(meta)
w = max(len(r[0]) for r in rows)
for leg, v, d in rows: print(f"{leg:<{w}}  {v:<7}  {d}")
reds = sum(1 for r in rows if r[1] == "RED")
print(f"VERDICT: {sum(1 for r in rows if r[1] == 'GREEN')} GREEN, {reds} RED, {sum(1 for r in rows if r[1] == 'UNKNOWN')} UNKNOWN of {len(rows)}")
sys.exit(2 if unknown else (1 if reds else 0))
