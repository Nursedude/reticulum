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

### `1.3.8+mf.11` → `1.3.9+mf.0` (2026-10-26, branch `port-1.3.9`, NOT tagged, NOT rolled)

Adopted upstream `1.3.9` — published 2026-07-19 as a **"Critical Security
Update" for rnsh** (details withheld upstream). Source of record: the PyPI
sdist `rns-1.3.9.tar.gz` (sha256 prefix `fd3745c1aba4dcbf`), since upstream
publishes off-GitHub; the 1.3.8→1.3.9 diff was taken between the two PyPI
sdists and applied with `patch -p2 -F0`.

- **Cut from the `1.3.8+mf.11` tag (`29c72f38`) — the code the fleet runs —
  NOT from the `meshforge` branch head**, which also carries the paused
  mf.6–mf.10 PipeInterface arc. Same side-release discipline as mf.11.
- **Why now (measured 2026-10-26):** rnsh ships inside this fork
  (`RNS/Utilities/rnsh/`) and its entry point is installed on 8 MeshForge boxes;
  0 rnsh processes on all 11 boxes (latent: exposure needs `rnsh -l`). Every
  1.3.9-changed file this fork had not patched was byte-identical to 1.3.8 —
  the fork carried none of the update.
- **Scope of 1.3.9** (22 files): rnsh (identity + `allowed_identities` now
  resolved under rnsh's own configdir), interface frame-length guards
  (Backbone/TCP drop frames > `HW_MTU` + IFAC and cap buffers at 2×MTU),
  Link/Resource/Transport/Discovery changes, new `LOG_PATHING` level replacing
  `Transport.PR_LOGLEVEL`.
- **The withheld fix, identified (non-author Fable read 2026-10-26, author
  re-read):** an **rnsh auth bypass → remote command execution** in 1.3.8.
  `rnsh/session.py:203-207` (1.3.8): a non-allowed identity calls
  `self.terminate(...)` and then falls through unconditionally to
  `self.remote_identity = identity; self._set_state(LSSTATE_WAIT_VERS)`, after
  which `VersionInfo → ExecuteCommand → _start_cmd` runs with no auth check —
  any identity could run the listener's command inside the prune window. 1.3.9
  gates every state on `authenticated` / `terminated` (`session.py:107,206,
  298-303`) and `Link.py:972` lets a link's identity be set only once (no
  post-auth swap). The fleet's exposure is latent (0 `rnsh -l` processes on
  11 boxes) but the vulnerable entry point is installed on 8.
- **Roll notes (same read):** (1) rnsh's identity MOVES from
  `<reticulum>/storage/identities/rnsh[.svc]` to `<rnsh configdir>/identity
  [.svc]`, and `-c` now means the rnsh dir (`--rnsconfig` = RNS dir) — an
  existing rnsh identity is silently abandoned, not migrated (none found on
  VolcanoAI; other boxes unchecked). (2) BackboneInterface gains a 12 h
  fast-flap IP ban and `rnstatus` a `Blocked : N IPs` line (the fleet runs no
  BackboneInterface; MeshForge's `rns_status_parser` ignores unknown lines).
  (3) An invalid resource advert now tears the link down; legit 1.3.8
  senders cannot trip the new 3 MiB check. (4) Upstream defects carried, fork
  `+mf.1` candidates, not blockers: config `loglevel` clamp still 7;
  `Discovery.py` `subprocess.run` has no timeout and a `dlat > 180` longitude
  typo; `BackboneInterface.py:826` dereferences a None `parent_interface`.
- **Wire-compat invariant:** `Packet.py` and `RNS/Cryptography/` are untouched
  by 1.3.9; IFAC key derivation keeps identical `hkdf(length=64, …,
  salt=ifac_salt, context=None)` arguments; `Resource.REJECTED` 0x00→0x09 is
  local status (it collided with `NONE`). BELIEVED from reading — the
  public-net interop proof below is the check.
- **Merge result:** 2 rejects, both trivial — `_version.py` (our marker) and
  `Transport.start`'s `PR_LOGLEVEL` assignment (removed as upstream did; no
  reader remains). 17 files are byte-identical to 1.3.9; the five fork-patched
  files carry exactly the same delta size vs 1.3.9 as they did vs 1.3.8
  (`Reticulum.py` 234, `__init__.py` 66, `Transport.py` 57, Backbone 2,
  `_version.py` 2) — no patch lost or grown. Markers intact: `_rpc_recv` 22,
  `DETACH_TIMEOUT` 9, `rpc_listener` 11.
- **Tests (this box, one process per file):** 13 `meshforge_*` files 0 failed;
  `tests.all` OK (skipped=1) ×2 — identical to the mf.11 baseline run the same
  way. ⚠️ `tests.link` packet tests are LOAD-SENSITIVE: under a concurrent full
  MeshForge suite both trees failed them (baseline 4/5, port 5/5). **Quiet-box
  interleaved A/B done 2026-10-26:** `tests.link` 5× each (base 5/5 pass, port
  4/5), then `test_03a/03b_packets` 10× each — **base 3/10 fail, port 3/10 fail**
  (loadavg 0.9-2.1). Combined base 3/15, port 4/15: indistinguishable — a
  pre-existing flake (Packet.py and the packet path are unchanged by 1.3.9),
  not a port regression.
- **Before roll (operator go):** quiet-box link A/B; tag `1.3.9+mf.0`; one-box
  canary (moc3) + wedge-probe/clean-stop soak + **public-net interop proof**;
  MeshAnchor follows in the same pair (MF-FORK-PIN in `requirements/rns.txt`,
  `parity_check`).


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

### `1.3.8+mf.11` (2026-10-20) — teardown releases `@rns/<instance>/rpc` too. ⚠️ CUT FROM `mf.5`, NOT from mf.10

**Read this before comparing version numbers.** `mf.11` is `1.3.8+mf.5`
plus ONE fix, and nothing else. It does **not** contain `mf.6`–`mf.10`, the
PipeInterface congestion arc: mf.6/mf.7 were tagged but never rolled, mf.7
measured RED on 4 KB-page kernels, and mf.8–mf.10 are untagged and paused.
The fix was cut from `mf.5`, the fleet pin, so the fleet takes this one change
without shipping that arc. "`mf.11` > `mf.7`" does NOT mean it carries mf.7.
Read the pinned source, never the version string.

Upstream never closed `rpc_listener`, and since mf.3 detach frees the
shared-instance name. So a process that outlived its teardown kept
`@rns/<n>/rpc` bound on its own. A new host then bound `@rns/<n>`, became
the shared instance, and died in `__init__` building its RPC `Listener` with
`OSError [Errno 98]`. Measured on the fleet's mf.5, as two processes with a
random instance name: rc=3 → rc=0 with this fix.

Cure: `__release_rpc_listener()` runs BEFORE `detach_interfaces()` on both
paths (`exit_handler` and the signal `__deferred_shutdown`). It calls
`shutdown()` and then `close()`: shutdown wakes the blocked `accept()`,
whose in-flight syscall would otherwise keep the name bound past `close()`.
`rpc_loop` exits once the listener is gone. If the private
`_listener._socket` is missing, it logs a LOG_WARNING. Tests:
`tests/meshforge_rpc_release.py` (AF_UNIX; asserts the RPC name is already
gone when detach begins, and that the rpc thread exits) and
`tests/meshforge_rpc_release_tcp.py` (TCP control port). Both are red on the
unpatched tree. Reviews: a hand-prompted subagent (SHIP-WITH-NOTES) and
`code-review medium` (8 findings, folded in). Local socket handling only; no
wire or crypto change.

Source: on the `meshforge` branch, commits `7672ff60` + `f4557272` +
`2eb170c9`, applied as their code+test diff onto `1.3.8+mf.5`. On this tree:
all 13 `meshforge_*` files rc=0, and `tests.all` ran 36, OK (skipped=1).

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
