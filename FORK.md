# MeshForge fork of Reticulum (RNS)

A fork of [Reticulum](https://github.com/markqvist/Reticulum) by Mark Qvist, maintained by
MeshForge (Nursedude / WH6GXZ) so RNS runs reliably in the MeshForge domain. Developed with
an AI assistant (Claude) under the maintainer's direction. The full record of every change,
measurement and decision is in [`FORK_HISTORY.md`](FORK_HISTORY.md).

## Now

- **Shipped:** `1.3.9+mf.0`, tag `e915dcf4` on branch `port-1.3.9`, rolled to the MeshForge
  fleet 2026-10-27. Base is upstream `1.3.9`, taken from the PyPI sdist because upstream
  publishes off-GitHub.
- **Branch `meshforge`** also carries a paused, unreleased PipeInterface arc (`mf.6`–`mf.10`).
  It is not in any shipped tag after `mf.5`.
- **Versions:** `<upstream base>+mf.N`. `N` resets to 0 on a new upstream base.

## Why

Upstream became a read-only mirror in December 2025: no issue tracker and no
security-disclosure channel. MeshForge depends on RNS and kept hitting a class of rnsd RPC
fragility. With the fork, a version bump is a reviewed decision, and fragility gets fixed
at the source.

## Rules

- **Never change the crypto primitives** (Ed25519 / X25519 / AES-256-CBC / Fernet) **or the
  packet / announce / path-table wire format.** That forks the network, not the code.
  Changes are limited to reliability fixes, packaging and provenance.
- **License:** the upstream Reticulum License is kept (`LICENSE`); our changes are listed in
  `NOTICE`. On its AI/ML-training-dataset clause:
  - No RNS or LXMF source goes into any dataset, eval fixture or fine-tune.
  - Model training is off on every account whose AI sessions read or patch this fork
    (operator-confirmed 2026-10-13).
  - The project is non-commercial.
  - MeshForge and MeshAnchor (GPL-3.0) combine with this fork under a GPL §7 additional
    permission (their `LICENSE-EXCEPTION.md`).

## Changes in `1.3.9+mf.0` — what / why / our solution

| Change | What broke | Our solution |
|---|---|---|
| Bounded RPC receive (#72) | A wedged rnsd hung every client: the RPC client used a bare blocking `recv` | Poll-then-receive `_rpc_recv`, used by all 21 client receive sites |
| Logging fallback | `log()`'s write-failure fallback re-entered its own lock | Plain `Lock`; the fallback re-logs after releasing it (an `RLock` flaked transfers) |
| `mf.1` AutoInterface | rnsd exited 255 when started inside the IPv6 DAD window (link-local still `tentative`) | The interface no longer re-binds an unusable address unguarded |
| `mf.2` `interface_mode = gateway` | `KeyError('mode')` killed rnsd at startup | Read the key the stanza actually sets |
| `mf.3` refused `require_shared_instance` | The refused client kept `@rns/<instance>` bound and became a squatter | Its local server interface releases the name on detach |
| `mf.4` join-only retry | A refusal latched the singleton, so a retry failed and `get_instance()` returned a half-built object | Clear the latch on refusal; `MF_REQUIRE_SHARED_RETRYABLE` marks the behaviour |
| `mf.5` external interface panic | A plugin calling `RNS.panic()` while loading took down rnsd and every interface | During plugin load, `panic()` raises `ExternalInterfacePanic`; only that interface is refused; its detach is bounded by `RNS_DETACH_TIMEOUT` |
| `mf.11` RPC name on teardown | A process that outlived its teardown kept `@rns/<instance>/rpc`, and the next host died with `EADDRINUSE` | `exit_handler()` releases the RPC listener too (`shutdown()` before `close()`) |
| Upstream `1.3.9` | The rnsh "critical security update": an auth bypass allowing remote command execution in 1.3.8 | Adopted from the PyPI sdist; interop proven against stock 1.3.9, direct and over the public network |

## Adopting a new upstream release

1. Merge the new release into a branch cut from the **shipped tag**, not the `meshforge` head.
2. Re-validate every carried patch against the new base; an auto-merge is not proof.
3. Public-network interop proof (a stock announce plus an LXMF round trip), a one-box
   canary, then the fleet roll.
4. Set the base to the new upstream version and reset `+mf` to 0.
