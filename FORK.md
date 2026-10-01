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

### Unreleased (next `+mf.N`) (2026-10-01) — a refused `require_shared_instance` client releases `@rns`

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
