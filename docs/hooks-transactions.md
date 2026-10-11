# pi-keysmith transaction reference

This document describes the durable transaction model `pi-keysmith` uses so an
interruption (such as `SIGKILL`) can be inspected and restored through explicit
`--recover`.

## Scope

`pi-keysmith` manages a single file per pi configuration directory:
`SYSTEM.md`. It does not touch `APPEND_SYSTEM.md`, `AGENTS.md`, `CLAUDE.md`,
pi's `settings.json`, `models.json`, or any pi binary. A confirmed deployment
writes `SYSTEM.md` and `.pi-keysmith-manifest.json` only, plus timestamped
backups of the prior `SYSTEM.md` and prior manifest when they exist.

## Channels and precedence

pi loads three instruction channels:

1. `SYSTEM.md` — the base system prompt. A global `~/.pi/agent/SYSTEM.md`
   completely replaces pi's default system prompt; pi discovers it with a plain
   existence check (no project-trust gate applies to the global path).
2. `APPEND_SYSTEM.md` — appended to the system prompt after `SYSTEM.md`.
3. `AGENTS.md` / `CLAUDE.md` — context files loaded after the append layer.

`pi-keysmith` deploys into channel 1 and leaves channels 2 and 3 untouched so
the persona card and global instructions continue to stack on top exactly as
pi's own loader dictates.

## Deployment state machine

Phases, in order:

1. `initializing` — private journal directory created; immutable `intent.json`
   and `journal.json` published.
2. `prepared` — preflight complete; before-state snapshots recorded.
3. `system-intent` — prior `SYSTEM.md` backed up (if present); new `SYSTEM.md`
   atomically published.
4. `manifest-intent` — prior manifest backed up (if present); new manifest
   atomically published; companion `manifest-intent.json` records its SHA-256.
5. `final-sweep` — live `SYSTEM.md` and manifest re-fingerprinted and matched
   against expectations.
6. `committed` — forward operation complete; cleanup intent published, journal
   and cleanup marker removed.

On any failure after `initializing`, the durable journal is left in place; no
automatic rollback is attempted (a hard kill cannot run Python). `--status`
reports the residue as a blocker and `--recover` restores the before state.

## Uninstall state machine

Mirrors deploy with phases `initializing`, `prepared`, `system-intent`,
`manifest-intent`, `final-sweep`, `committed`. The forward operation restores
`SYSTEM.md` to its pre-deploy state (or removes it if it was created by the
deploy), archives the current manifest as `.pi-keysmith-manifest.json.uninstalled_<ts>`,
and restores the previous manifest layer if one was recorded.

## Recovery

`--recover` (default preview; `--yes` to apply):

- discovers `.pi-keysmith-transaction-<id>/` journals across the selected
  directories;
- dispatches by the journal's immutable `operation` (`deploy` or `uninstall`);
- requires a single transaction id across all selected participants;
- preflights that every live managed resource is either its recorded `before`
  state, one of its allowed `after` states, or absent when allowed;
- on `--yes`, reverses participants and restores resources in order
  `manifest -> system` from before-state snapshots, then cleans up the journal.

Mixed deploy/uninstall transactions in one recovery scope are refused; select a
participant of each transaction separately.

## Filesystem boundaries

- No symlinks are followed; managed targets must be regular files.
- File contents are `fsync`ed; publication uses same-volume atomic replace.
- Private journals are `0700`; private files are `0600` (POSIX). Windows fresh
  deployment is `EXPLICIT_BETA` with a CLI warning and is not a formal support
  claim.

## Limits

The journal, intent, companion, manifest, and cleanup-marker evidence protects
against accidental drift and ordinary races. It is not cryptographic
authentication against coordinated same-user tampering, extreme power loss,
devices that ignore flush, or filesystem corruption.
