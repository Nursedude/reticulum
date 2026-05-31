# MeshForge fork of Reticulum (RNS)

This is a maintained fork of [Reticulum](https://github.com/markqvist/Reticulum)
by Mark Qvist, owned by the MeshForge project (Nursedude).

## Base

- **Upstream:** `markqvist/Reticulum`
- **Forked at tag:** `1.2.5`
- **Base commit:** `e8d161c0d50cc0416c98dcd1cee44807e7c52df1`
- **Fork branch:** `meshforge`
- **Version scheme:** PEP 440 local marker on the upstream base —
  `1.2.5+mf.0`, `1.2.5+mf.1`, … The base version (`1.2.5`) is never
  changed independently of upstream; only the `+mf.N` segment increments
  for MeshForge changes.

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
