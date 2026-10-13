"""Verdict table for accept.sh: reads <outdir>/<run>.log + .rc and applies the
HEALTHY readings named in accept.sh's header. Exit 0 = every leg GREEN, 1 = a
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
rows = []; unknown = False
def row(leg, ok, detail):
    global unknown
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

meta = open(os.path.join(OUT, "meta")).read().strip() if os.path.exists(os.path.join(OUT, "meta")) else "meta missing"
print(meta)
w = max(len(r[0]) for r in rows)
for leg, v, d in rows: print(f"{leg:<{w}}  {v:<7}  {d}")
reds = sum(1 for r in rows if r[1] == "RED")
print(f"VERDICT: {sum(1 for r in rows if r[1] == 'GREEN')} GREEN, {reds} RED, {sum(1 for r in rows if r[1] == 'UNKNOWN')} UNKNOWN of {len(rows)}")
sys.exit(2 if unknown else (1 if reds else 0))
